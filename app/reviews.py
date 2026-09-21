"""危险品配载交接审核：草稿—命令—确认的版本化存储。

设计要点：

- ``POST /reviews`` 以 (舱位, 货项, commandId) 建立 ``revision=1`` 的草稿，
  保存规范化请求（货项按编号排序）与复用规则引擎得到的裁决；
- ``POST /reviews/{id}/commands`` 支持 ``REPLACE_ITEMS``（替换货项并把
  版本号加一）与 ``CONFIRM``（冻结当前快照）；
- 每个成功命令先按 ``commandId`` 全局判重：同标识同内容原样重放首次成功
  响应（字节一致），同标识不同内容返回 ``COMMAND_ID_REUSED``；失败命令
  （404/409/400）不写入任何索引，因而冲突命令可用同一 commandId 携带新的
  ``expectedRevision`` 重试；
- 判重检查、状态检查与写入在同一个 ``BEGIN IMMEDIATE`` 事务中完成。

多工作进程部署（``uvicorn --workers N``）下，每个进程都持有指向同一个
SQLite 文件的连接；进程内用一把锁串行化对连接的使用，进程间由 SQLite
的写锁互斥。关键在于**不缓存**判重/版本状态：命令判重、审核与版本的
读取全部发生在取得写锁之后的事务内（read-your-writes），因此并发落到
不同工作进程的同标识请求只会有一个插入成功，其余事务在唯一约束上失败
后重新打开事务、读取获胜者写入的记录并原样重放；争用同一版本的不同
命令同样只有一个原子成功。数据库以 WAL 模式打开，提交即落盘，容器
重建后从文件完整恢复，判重与重放语义不变。

数据库路径由环境变量 ``STOWAGE_DB_PATH`` 指定；缺省（如本地测试）为
``:memory:``（单进程内存库）。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable

from app.rules import assess
from app.validation import ACTION_CONFIRM, ACTION_REPLACE_ITEMS, ApiError

DRAFT = "DRAFT"
CONFIRMED = "CONFIRMED"

# 持久化数据库路径环境变量；未配置时退化为进程内内存库（测试用）。
DB_PATH_ENV = "STOWAGE_DB_PATH"
DEFAULT_DB_PATH = ":memory:"

# 唯一约束冲突（理论上只会在跨进程同 commandId 竞争时出现一次）后，
# 重新打开事务重读获胜记录的最大次数。
MAX_COMMIT_RETRIES = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    hold      TEXT NOT NULL,
    status    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    review_id    TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    hold         TEXT NOT NULL,
    items_json   TEXT NOT NULL,
    command_id   TEXT NOT NULL,
    action       TEXT NOT NULL,
    verdict_json TEXT NOT NULL,
    PRIMARY KEY (review_id, revision)
);
CREATE TABLE IF NOT EXISTS commands (
    command_id     TEXT PRIMARY KEY,
    canonical_json TEXT NOT NULL,
    review_id      TEXT NOT NULL,
    status_code    INTEGER NOT NULL,
    body           BLOB NOT NULL
);
"""


def _canonical_items(items: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """规范化货项：按编号排序，使录入次序不影响判重与快照。"""
    return tuple(sorted(items, key=lambda item: item[0]))


def _jsonable(value: object) -> object:
    """把规范化元组递归转为 JSON 可序列化的列表。"""
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _canonical_key(canonical: tuple) -> str:
    """规范化内容的稳定序列化形式，用于跨进程/重启的判重比较。"""
    return json.dumps(
        _jsonable(canonical),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _render_body(content: dict) -> bytes:
    """按 ``JSONResponse`` 相同的设置渲染响应体，保证重放字节一致。"""
    return json.dumps(
        content, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _items_json(items: tuple[tuple[str, str], ...]) -> str:
    return json.dumps([[item_id, category] for item_id, category in items],
                      ensure_ascii=False)


def _load_items(items_json: str) -> tuple[tuple[str, str], ...]:
    return tuple((entry[0], entry[1]) for entry in json.loads(items_json))


def _review_body(
    review_id: str,
    revision: int,
    status: str,
    command_id: str,
    hold: str,
    items: tuple[tuple[str, str], ...],
    verdict: dict,
) -> bytes:
    """构造审核响应体并渲染为字节（与 JSONResponse 设置一致）。"""
    return _render_body(
        {
            "reviewId": review_id,
            "revision": revision,
            "status": status,
            "commandId": command_id,
            "hold": hold,
            "items": [
                {"id": item_id, "category": category}
                for item_id, category in items
            ],
            "conclusion": verdict["conclusion"],
            "evidence": verdict["evidence"],
        }
    )


class ReviewStore:
    """线程安全、可持久化、支持多工作进程共享同一文件的审核存储。

    所有公开方法都是原子操作：进程内由 :attr:`_lock` 串行化对单个 SQLite
    连接的使用，进程间由 ``BEGIN IMMEDIATE`` 取得的写锁互斥；判重与状态
    读取均在写事务内完成，唯一约束是跨进程判重的最终防线。
    """

    def __init__(self, db_path: str | None = None) -> None:
        path = (
            db_path
            if db_path is not None
            else os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)
        )
        if path != DEFAULT_DB_PATH:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db_path = path
        # 进程内锁：sqlite3 连接被多线程共享，必须保证 BEGIN/COMMIT 不被
        # 同进程的其他线程穿插；跨进程互斥由 SQLite 写锁负责。
        self._lock = threading.Lock()
        # isolation_level=None：自行控制 BEGIN IMMEDIATE/COMMIT；timeout 与
        # busy_timeout 让写锁争用等待而非立即报错。
        self._db = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=5.0
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        if path != DEFAULT_DB_PATH:
            # WAL：多个工作进程可同时持有读锁、单写者不阻塞读取；
            # NORMAL 在 WAL 下对进程崩溃/容器重建安全（提交已进入 WAL）。
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- 事务基元 -------------------------------------------------------

    def _transact(
        self, mutate: Callable[[sqlite3.Connection], tuple[int, bytes]]
    ) -> tuple[int, bytes]:
        """在进程锁内运行一次 ``BEGIN IMMEDIATE`` 写事务。

        ``mutate`` 必须在事务内先做判重与状态读取、再写入，并返回
        ``(状态码, 响应字节)``；它抛出的 :class:`ApiError` 会原样上抛
        （已回滚，不重试——失败命令不占标识）。遇到跨进程竞争导致的
        ``IntegrityError``（唯一约束）或锁/提交相关 ``OperationalError``
        则回滚并整段重跑，重新读取获胜者已提交的记录；保证连接不会
        停留在未结束的事务中。
        """
        last_error: sqlite3.Error | None = None
        for _ in range(MAX_COMMIT_RETRIES):
            with self._lock:
                try:
                    self._db.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    # 写锁等待 busy_timeout 后仍未取得：尚未开启事务，重试。
                    last_error = exc
                    continue

                committed = False
                try:
                    result = mutate(self._db)
                    self._db.execute("COMMIT")
                    committed = True
                    return result
                except ApiError:
                    raise
                except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
                    # IntegrityError：另一工作进程抢先提交了同 commandId；
                    # OperationalError：提交/读写阶段的锁等瞬时错误。
                    # 回滚后整段重跑，mutate 会读到最新已提交状态。
                    last_error = exc
                    continue
                finally:
                    if not committed:
                        with contextlib.suppress(sqlite3.Error):
                            self._db.execute("ROLLBACK")
        assert last_error is not None
        raise last_error

    @staticmethod
    def _existing_command(
        db: sqlite3.Connection, command_id: str
    ) -> sqlite3.Row | None:
        """必须在写事务内调用：读到的一定是已提交的最新命令记录。"""
        return db.execute(
            "SELECT command_id, canonical_json, review_id, status_code, body "
            "FROM commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()

    @staticmethod
    def _replay_existing(existing: sqlite3.Row, canonical_key: str) -> tuple[int, bytes]:
        """命中判重：内容一致原样重放，不一致 → 409 COMMAND_ID_REUSED。"""
        if existing["canonical_json"] != canonical_key:
            raise ApiError(
                "COMMAND_ID_REUSED",
                f"Command id '{existing['command_id']}' was already used with a "
                "different request.",
                status=409,
            )
        return existing["status_code"], bytes(existing["body"])

    # ---- 建草稿 ---------------------------------------------------------

    def create_review(
        self, hold: str, items: list[tuple[str, str]], command_id: str
    ) -> tuple[int, bytes]:
        canonical_items = _canonical_items(items)
        # CREATE 标记确保同一 commandId 不能跨建草稿与下命令混用。
        canonical_key = _canonical_key(("CREATE", hold, canonical_items))

        def mutate(db: sqlite3.Connection) -> tuple[int, bytes]:
            existing = self._existing_command(db, command_id)
            if existing is not None:
                return self._replay_existing(existing, canonical_key)

            review_id = uuid.uuid4().hex
            verdict = assess(list(canonical_items))
            body = _review_body(
                review_id, 1, DRAFT, command_id, hold, canonical_items, verdict
            )
            db.execute(
                "INSERT INTO reviews (review_id, hold, status) VALUES (?, ?, ?)",
                (review_id, hold, DRAFT),
            )
            db.execute(
                "INSERT INTO revisions (review_id, revision, hold, items_json, "
                "command_id, action, verdict_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id, 1, hold, _items_json(canonical_items),
                    command_id, "CREATE",
                    json.dumps(verdict, ensure_ascii=False),
                ),
            )
            db.execute(
                "INSERT INTO commands (command_id, canonical_json, review_id, "
                "status_code, body) VALUES (?, ?, ?, ?, ?)",
                (command_id, canonical_key, review_id, 201, body),
            )
            return 201, body

        return self._transact(mutate)

    # ---- 下命令 ---------------------------------------------------------

    def apply_command(
        self,
        review_id: str,
        command_id: str,
        action: str,
        expected_revision: int,
        items: list[tuple[str, str]] | None,
    ) -> tuple[int, bytes]:
        if action == ACTION_REPLACE_ITEMS:
            assert items is not None
            canonical = (
                "REPLACE_ITEMS",
                review_id,
                expected_revision,
                _canonical_items(items),
            )
        else:
            canonical = ("CONFIRM", review_id, expected_revision)
        canonical_key = _canonical_key(canonical)

        def mutate(db: sqlite3.Connection) -> tuple[int, bytes]:
            # 判重先于一切状态检查，且在写事务内读最新已提交记录。
            existing = self._existing_command(db, command_id)
            if existing is not None:
                return self._replay_existing(existing, canonical_key)

            review = db.execute(
                "SELECT hold, status FROM reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            # 新命令的固定报错顺序：
            # REVIEW_NOT_FOUND → REVISION_CONFLICT → REVIEW_FINALIZED。
            if review is None:
                raise ApiError(
                    "REVIEW_NOT_FOUND",
                    f"Review '{review_id}' does not exist.",
                    status=404,
                )
            current = db.execute(
                "SELECT revision, hold, items_json, verdict_json "
                "FROM revisions WHERE review_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (review_id,),
            ).fetchone()
            if expected_revision != current["revision"]:
                raise ApiError(
                    "REVISION_CONFLICT",
                    f"Expected revision {expected_revision} but review "
                    f"'{review_id}' is at revision {current['revision']}.",
                    status=409,
                )
            if review["status"] == CONFIRMED:
                raise ApiError(
                    "REVIEW_FINALIZED",
                    f"Review '{review_id}' is already confirmed and frozen.",
                    status=409,
                )

            if action == ACTION_REPLACE_ITEMS:
                assert items is not None
                new_items = _canonical_items(items)
                new_revision = current["revision"] + 1
                verdict = assess(list(new_items))
                body = _review_body(
                    review_id, new_revision, DRAFT, command_id,
                    review["hold"], new_items, verdict,
                )
                db.execute(
                    "INSERT INTO revisions (review_id, revision, hold, "
                    "items_json, command_id, action, verdict_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        review_id, new_revision, review["hold"],
                        _items_json(new_items), command_id,
                        ACTION_REPLACE_ITEMS,
                        json.dumps(verdict, ensure_ascii=False),
                    ),
                )
            else:
                # 确认不推进版本号，只冻结当前快照。
                frozen_items = _load_items(current["items_json"])
                verdict = json.loads(current["verdict_json"])
                body = _review_body(
                    review_id, current["revision"], CONFIRMED, command_id,
                    current["hold"], frozen_items, verdict,
                )
                db.execute(
                    "UPDATE reviews SET status = ? WHERE review_id = ?",
                    (CONFIRMED, review_id),
                )

            db.execute(
                "INSERT INTO commands (command_id, canonical_json, review_id, "
                "status_code, body) VALUES (?, ?, ?, ?, ?)",
                (command_id, canonical_key, review_id, 200, body),
            )
            return 200, body

        return self._transact(mutate)


# 进程级单例存储。
store = ReviewStore()
