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
- 判重检查、状态检查与写入在同一次 SQLite ``BEGIN IMMEDIATE`` 事务内完成，
  争用同一版本的多个命令只有一个原子成功，其余得到 409 且不留下部分状态。

多工作进程部署（``uvicorn --workers N``）下各进程不共享内存锁与缓存，因此
SQLite 文件是唯一事实来源：

- 每次操作都先取得库级写锁（``BEGIN IMMEDIATE``），随后才做判重、版本检查
  与写入；进程间由 SQLite 的写锁串行化，``busy_timeout`` 让后来者等待而非
  立刻失败；
- 同一 ``commandId`` 的并发请求即便都未在事务内读到已有记录，也只有一个
  进程能插入 ``commands`` 主键：落败方收到 ``IntegrityError`` 后回滚并改读
  获胜方已提交的记录——内容一致则原样重放其状态码与响应字节，不一致则
  返回 ``409 COMMAND_ID_REUSED``，绝不产生 500；
- 不保留任何进程内状态缓存，审核状态、当前版本与判重记录每次从事务内读取，
  因而进程切换与容器重建后语义不变。

审核、版本快照、确认状态与成功命令的判重记录都写入 SQLite（标准库，
无外部依赖）：每次成功的状态变更与命令记录在同一个 ``BEGIN IMMEDIATE``
事务中提交，进程重启后从磁盘完整恢复，判重与重放语义不变。数据库路径由
环境变量 ``STOWAGE_DB_PATH`` 指定；缺省（如本地测试）为 ``:memory:``。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator

from app.rules import assess
from app.validation import ACTION_CONFIRM, ACTION_REPLACE_ITEMS, ApiError

DRAFT = "DRAFT"
CONFIRMED = "CONFIRMED"

# 持久化数据库路径环境变量；未配置时退化为进程内内存库（测试用）。
DB_PATH_ENV = "STOWAGE_DB_PATH"
DEFAULT_DB_PATH = ":memory:"

# 跨进程写锁争用时的等待上限：所有事务都很小，正常情况下远在超时前完成。
BUSY_TIMEOUT_MS = 30_000

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
    """规范化内容的稳定序列化形式，用于跨进程/跨重启的判重比较。"""
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


def _review_body(
    review_id: str,
    status: str,
    revision: int,
    hold: str,
    items: tuple[tuple[str, str], ...],
    verdict: dict,
    command_id: str,
) -> dict:
    """构造审核响应内容（结构与建草稿/命令响应契约一致）。"""
    return {
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


class ReviewStore:
    """跨进程、线程安全、可持久化的审核存储；所有公开方法均为原子操作。

    SQLite 文件是唯一事实来源，不保留任何进程内状态缓存：每个工作进程持有
    各自的连接与进程内线程锁，进程间的互斥与判重完全由
    ``BEGIN IMMEDIATE`` 写锁和 ``commands`` 主键保证，因此工作进程数可任意
    配置，进程切换与容器重建不影响判重、版本与重放语义。
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
        # 进程内锁只负责串行化同一连接上的多线程访问；跨进程互斥由
        # SQLite 的 BEGIN IMMEDIATE 写锁保证。
        self._lock = threading.RLock()
        # 单连接 + check_same_thread=False：:memory: 库因此可被同一进程的
        # 多线程（测试服务器）共享；文件库下每个工作进程各有独立连接。
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self._db.executescript(_SCHEMA)

    # ---- 事务与状态读取 -------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        # IMMEDIATE 立即取得库级写锁：判重检查与写入在同一事务内，其他进程
        # 的写入无法穿插，保证“仅一个原子成功”且提交即落盘。
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def _command_row(self, command_id: str) -> tuple | None:
        return self._db.execute(
            "SELECT canonical_json, status_code, body FROM commands "
            "WHERE command_id = ?",
            (command_id,),
        ).fetchone()

    @staticmethod
    def _replay_row(row: tuple, command_id: str, canonical_key: str) -> tuple[int, bytes]:
        """按已提交的命令记录决定重放或判重冲突。"""
        stored_key, status_code, body = row
        if stored_key != canonical_key:
            raise ApiError(
                "COMMAND_ID_REUSED",
                f"Command id '{command_id}' was already used with a "
                "different request.",
                status=409,
            )
        return status_code, bytes(body)

    def _stored_replay(
        self, command_id: str, canonical_key: str
    ) -> tuple[int, bytes] | None:
        """事务内全局判重：命中且内容一致则重放，内容不一致则 409。"""
        row = self._command_row(command_id)
        if row is None:
            return None
        return self._replay_row(row, command_id, canonical_key)

    def _replay_after_race(
        self, command_id: str, canonical_key: str
    ) -> tuple[int, bytes]:
        """主键冲突落败后的改判：读取获胜进程已提交的命令记录。

        回滚后连接回到自动提交态，直接读取获胜方的提交结果；内容一致则重放
        其字节，不一致则 409。``reviews`` 主键为 UUID、版本号在写锁内由
        ``MAX+1`` 得出，因此这里能且只能读到 ``commands`` 记录。
        """
        row = self._command_row(command_id)
        if row is None:  # 理论上不可达：唯一冲突源即 commands 主键。
            raise RuntimeError(
                f"Lost write race for command id {command_id!r} but found no "
                "commanded record on re-read"
            )
        return self._replay_row(row, command_id, canonical_key)

    def _review_row(self, review_id: str) -> tuple[str, str] | None:
        """返回 ``(hold, status)``；每次事务内现读，跨进程始终最新。"""
        return self._db.execute(
            "SELECT hold, status FROM reviews WHERE review_id = ?",
            (review_id,),
        ).fetchone()

    def _latest_revision_row(self, review_id: str) -> tuple:
        return self._db.execute(
            "SELECT revision, hold, items_json, verdict_json FROM revisions "
            "WHERE review_id = ? ORDER BY revision DESC LIMIT 1",
            (review_id,),
        ).fetchone()

    def _insert_revision(
        self,
        review_id: str,
        revision: int,
        hold: str,
        items: tuple[tuple[str, str], ...],
        command_id: str,
        action: str,
        verdict: dict,
    ) -> None:
        items_json = json.dumps(
            [[item_id, category] for item_id, category in items],
            ensure_ascii=False,
        )
        verdict_json = json.dumps(verdict, ensure_ascii=False)
        self._db.execute(
            "INSERT INTO revisions (review_id, revision, hold, items_json, "
            "command_id, action, verdict_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                review_id,
                revision,
                hold,
                items_json,
                command_id,
                action,
                verdict_json,
            ),
        )

    def _insert_command(
        self,
        command_id: str,
        canonical_key: str,
        review_id: str,
        status_code: int,
        body: bytes,
    ) -> None:
        self._db.execute(
            "INSERT INTO commands (command_id, canonical_json, review_id, "
            "status_code, body) VALUES (?, ?, ?, ?, ?)",
            (command_id, canonical_key, review_id, status_code, body),
        )

    # ---- 建草稿 ---------------------------------------------------------

    def create_review(
        self, hold: str, items: list[tuple[str, str]], command_id: str
    ) -> tuple[int, bytes]:
        canonical_items = _canonical_items(items)
        # CREATE 标记确保同一 commandId 不能跨建草稿与下命令混用。
        canonical_key = _canonical_key(("CREATE", hold, canonical_items))
        with self._lock:
            try:
                with self._transaction():
                    # 判重先于一切状态检查。
                    replay = self._stored_replay(command_id, canonical_key)
                    if replay is not None:
                        return replay

                    review_id = uuid.uuid4().hex
                    verdict = assess(list(canonical_items))
                    body = _render_body(
                        _review_body(
                            review_id,
                            DRAFT,
                            1,
                            hold,
                            canonical_items,
                            verdict,
                            command_id,
                        )
                    )
                    self._db.execute(
                        "INSERT INTO reviews (review_id, hold, status) "
                        "VALUES (?, ?, ?)",
                        (review_id, hold, DRAFT),
                    )
                    self._insert_revision(
                        review_id,
                        1,
                        hold,
                        canonical_items,
                        command_id,
                        "CREATE",
                        verdict,
                    )
                    # 与状态变更同事务落盘：本插入的主键是跨进程原子仲裁点。
                    self._insert_command(
                        command_id, canonical_key, review_id, 201, body
                    )
                return 201, body
            except sqlite3.IntegrityError:
                # 另一进程已用同一 commandId 提交：回滚后重放其结果。
                return self._replay_after_race(command_id, canonical_key)

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
            canonical_items = _canonical_items(items)
            canonical_key = _canonical_key(
                (
                    ACTION_REPLACE_ITEMS,
                    review_id,
                    expected_revision,
                    canonical_items,
                )
            )
        else:
            canonical_items = None
            canonical_key = _canonical_key(
                (ACTION_CONFIRM, review_id, expected_revision)
            )

        with self._lock:
            try:
                with self._transaction():
                    # 判重先于一切状态检查（含 REVIEW_NOT_FOUND）。
                    replay = self._stored_replay(command_id, canonical_key)
                    if replay is not None:
                        return replay

                    # 新命令的固定报错顺序：
                    # REVIEW_NOT_FOUND → REVISION_CONFLICT → REVIEW_FINALIZED。
                    # 全部状态从事务内现读，保证看到其他进程已提交的版本。
                    review_row = self._review_row(review_id)
                    if review_row is None:
                        raise ApiError(
                            "REVIEW_NOT_FOUND",
                            f"Review '{review_id}' does not exist.",
                            status=404,
                        )
                    hold, status = review_row
                    revision_no, _rev_hold, items_json, verdict_json = (
                        self._latest_revision_row(review_id)
                    )
                    if expected_revision != revision_no:
                        raise ApiError(
                            "REVISION_CONFLICT",
                            f"Expected revision {expected_revision} but review "
                            f"'{review_id}' is at revision {revision_no}.",
                            status=409,
                        )
                    if status == CONFIRMED:
                        raise ApiError(
                            "REVIEW_FINALIZED",
                            f"Review '{review_id}' is already confirmed and "
                            "frozen.",
                            status=409,
                        )

                    if action == ACTION_REPLACE_ITEMS:
                        assert canonical_items is not None
                        verdict = assess(list(canonical_items))
                        new_revision = revision_no + 1
                        body = _render_body(
                            _review_body(
                                review_id,
                                DRAFT,
                                new_revision,
                                hold,
                                canonical_items,
                                verdict,
                                command_id,
                            )
                        )
                        # 写锁内 MAX+1 得出新版本号，并发替换不可能撞号；
                        # 落败方会在上面的版本检查处得到 REVISION_CONFLICT。
                        self._insert_revision(
                            review_id,
                            new_revision,
                            hold,
                            canonical_items,
                            command_id,
                            ACTION_REPLACE_ITEMS,
                            verdict,
                        )
                        self._insert_command(
                            command_id, canonical_key, review_id, 200, body
                        )
                        return 200, body

                    # 确认不推进版本号，只冻结当前快照。
                    current_items = tuple(
                        (entry[0], entry[1]) for entry in json.loads(items_json)
                    )
                    current_verdict = json.loads(verdict_json)
                    body = _render_body(
                        _review_body(
                            review_id,
                            CONFIRMED,
                            revision_no,
                            hold,
                            current_items,
                            current_verdict,
                            command_id,
                        )
                    )
                    self._db.execute(
                        "UPDATE reviews SET status = ? WHERE review_id = ?",
                        (CONFIRMED, review_id),
                    )
                    self._insert_command(
                        command_id, canonical_key, review_id, 200, body
                    )
                    return 200, body
            except sqlite3.IntegrityError:
                # 另一进程已用同一 commandId 提交：回滚后重放其结果。
                return self._replay_after_race(command_id, canonical_key)


# 进程级单例存储。多工作进程部署时每个进程各自实例化，共享同一 SQLite 文件。
store = ReviewStore()
