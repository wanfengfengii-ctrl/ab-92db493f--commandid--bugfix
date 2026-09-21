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
- 判重检查、状态检查与写入在同一把进程内锁内一次完成，争用同一版本的
  多个命令只有一个原子成功，其余得到 409 且不留下部分状态。

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
from collections.abc import Callable, Iterator

from app.rules import assess
from app.validation import ACTION_CONFIRM, ACTION_REPLACE_ITEMS, ApiError

DRAFT = "DRAFT"
CONFIRMED = "CONFIRMED"

# 持久化数据库路径环境变量；未配置时退化为进程内内存库（测试用）。
DB_PATH_ENV = "STOWAGE_DB_PATH"
DEFAULT_DB_PATH = ":memory:"

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
    """规范化内容的稳定序列化形式，用于跨重启的判重比较。"""
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


class _Revision:
    """某一版本的规范化请求与裁决快照。"""

    def __init__(
        self,
        revision: int,
        hold: str,
        items: tuple[tuple[str, str], ...],
        command_id: str,
        action: str,
        verdict: dict | None = None,
    ) -> None:
        self.revision = revision
        self.hold = hold
        self.items = items
        self.command_id = command_id
        self.action = action
        # 重启恢复时裁决按冻结快照原样载入；新快照复用规则引擎计算。
        self.verdict = verdict if verdict is not None else assess(list(items))


class _Review:
    def __init__(
        self,
        review_id: str,
        hold: str,
        status: str,
        revisions: list[_Revision],
    ) -> None:
        self.review_id = review_id
        self.hold = hold
        self.status = status
        self.revisions: list[_Revision] = revisions

    @classmethod
    def new_draft(
        cls,
        review_id: str,
        hold: str,
        items: tuple[tuple[str, str], ...],
        command_id: str,
    ) -> "_Review":
        return cls(review_id, hold, DRAFT, [_Revision(1, hold, items, command_id, "CREATE")])

    @property
    def revision(self) -> int:
        return self.revisions[-1].revision

    @property
    def current(self) -> _Revision:
        return self.revisions[-1]


class _CommandRecord:
    """一次成功命令的判重记录：规范化内容键 + 首次响应字节。"""

    def __init__(
        self, canonical_key: str, review_id: str, status_code: int, body: bytes
    ) -> None:
        self.canonical_key = canonical_key
        self.review_id = review_id
        self.status_code = status_code
        self.body = body


class ReviewStore:
    """线程安全、可持久化的审核存储；所有公开方法均为原子操作。"""

    def __init__(self, db_path: str | None = None) -> None:
        path = (
            db_path
            if db_path is not None
            else os.environ.get(DB_PATH_ENV, DEFAULT_DB_PATH)
        )
        if path != DEFAULT_DB_PATH:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db_path = path
        self._lock = threading.RLock()
        self._reviews: dict[str, _Review] = {}
        self._commands: dict[str, _CommandRecord] = {}
        # 单进程 uvicorn 部署：进程内 RLock 串行化，SQLite 事务负责落盘与
        # 原子可见性；isolation_level=None 以便显式控制 BEGIN/COMMIT。
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)
        self._load()

    # ---- 持久化 ---------------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        # IMMEDIATE 立即取得写锁：判重检查与写入在同一事务内，进程内锁之外
        # 也不会有任何写入穿插，保证“仅一个原子成功”且提交即落盘。
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def _load(self) -> None:
        """启动时从磁盘恢复全部审核快照与成功命令判重记录。"""
        revisions_by_review: dict[str, list[_Revision]] = {}
        rows = self._db.execute(
            "SELECT review_id, revision, hold, items_json, command_id, action, "
            "verdict_json FROM revisions ORDER BY review_id, revision"
        ).fetchall()
        for (
            review_id,
            revision,
            hold,
            items_json,
            command_id,
            action,
            verdict_json,
        ) in rows:
            items = tuple(
                (entry[0], entry[1]) for entry in json.loads(items_json)
            )
            verdict = json.loads(verdict_json)
            revisions_by_review.setdefault(review_id, []).append(
                _Revision(revision, hold, items, command_id, action, verdict)
            )

        for review_id, hold, status in self._db.execute(
            "SELECT review_id, hold, status FROM reviews"
        ).fetchall():
            self._reviews[review_id] = _Review(
                review_id, hold, status, revisions_by_review.get(review_id, [])
            )

        for command_id, canonical_json, review_id, status_code, body in self._db.execute(
            "SELECT command_id, canonical_json, review_id, status_code, body "
            "FROM commands"
        ).fetchall():
            self._commands[command_id] = _CommandRecord(
                canonical_json, review_id, status_code, bytes(body)
            )

    @staticmethod
    def _revision_params(review_id: str, revision: _Revision) -> tuple:
        items_json = json.dumps(
            [list(item) for item in revision.items], ensure_ascii=False
        )
        verdict_json = json.dumps(revision.verdict, ensure_ascii=False)
        return (
            review_id,
            revision.revision,
            revision.hold,
            items_json,
            revision.command_id,
            revision.action,
            verdict_json,
        )

    def _persist_success(
        self,
        command_id: str,
        canonical_key: str,
        review: _Review,
        status_code: int,
        body: bytes,
        db_writes: Callable[[], None],
    ) -> None:
        """状态变更与命令记录在同一事务落盘，随后才更新内存索引。"""
        with self._transaction():
            db_writes()
            self._db.execute(
                "INSERT INTO commands (command_id, canonical_json, review_id, "
                "status_code, body) VALUES (?, ?, ?, ?, ?)",
                (command_id, canonical_key, review.review_id, status_code, body),
            )
        self._commands[command_id] = _CommandRecord(
            canonical_key, review.review_id, status_code, body
        )

    # ---- 响应渲染 -------------------------------------------------------

    @staticmethod
    def _body_for(
        review: _Review,
        command_id: str,
        revision: _Revision | None = None,
        status: str | None = None,
    ) -> dict:
        current = revision if revision is not None else review.current
        return {
            "reviewId": review.review_id,
            "revision": current.revision,
            "status": status if status is not None else review.status,
            "commandId": command_id,
            "hold": current.hold,
            "items": [
                {"id": item_id, "category": category}
                for item_id, category in current.items
            ],
            "conclusion": current.verdict["conclusion"],
            "evidence": current.verdict["evidence"],
        }

    def _replay_or_reject(
        self, command_id: str, canonical_key: str
    ) -> tuple[int, bytes] | None:
        """全局判重：命中且内容一致则重放，内容不一致则 409。"""
        existing = self._commands.get(command_id)
        if existing is None:
            return None
        if existing.canonical_key != canonical_key:
            raise ApiError(
                "COMMAND_ID_REUSED",
                f"Command id '{command_id}' was already used with a "
                "different request.",
                status=409,
            )
        return existing.status_code, existing.body

    # ---- 建草稿 ---------------------------------------------------------

    def create_review(
        self, hold: str, items: list[tuple[str, str]], command_id: str
    ) -> tuple[int, bytes]:
        canonical_items = _canonical_items(items)
        # CREATE 标记确保同一 commandId 不能跨建草稿与下命令混用。
        canonical = ("CREATE", hold, canonical_items)
        canonical_key = _canonical_key(canonical)
        with self._lock:
            replay = self._replay_or_reject(command_id, canonical_key)
            if replay is not None:
                return replay

            review_id = uuid.uuid4().hex
            review = _Review.new_draft(review_id, hold, canonical_items, command_id)
            body = _render_body(self._body_for(review, command_id))

            def db_writes() -> None:
                self._db.execute(
                    "INSERT INTO reviews (review_id, hold, status) VALUES (?, ?, ?)",
                    (review_id, hold, DRAFT),
                )
                self._db.execute(
                    "INSERT INTO revisions (review_id, revision, hold, items_json, "
                    "command_id, action, verdict_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    self._revision_params(review_id, review.current),
                )

            self._persist_success(command_id, canonical_key, review, 201, body, db_writes)
            self._reviews[review_id] = review
            return 201, body

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

        with self._lock:
            replay = self._replay_or_reject(command_id, canonical_key)
            if replay is not None:
                return replay

            # 新命令的固定报错顺序：
            # REVIEW_NOT_FOUND → REVISION_CONFLICT → REVIEW_FINALIZED。
            review = self._reviews.get(review_id)
            if review is None:
                raise ApiError(
                    "REVIEW_NOT_FOUND",
                    f"Review '{review_id}' does not exist.",
                    status=404,
                )
            if expected_revision != review.revision:
                raise ApiError(
                    "REVISION_CONFLICT",
                    f"Expected revision {expected_revision} but review "
                    f"'{review_id}' is at revision {review.revision}.",
                    status=409,
                )
            if review.status == CONFIRMED:
                raise ApiError(
                    "REVIEW_FINALIZED",
                    f"Review '{review_id}' is already confirmed and frozen.",
                    status=409,
                )

            if action == ACTION_REPLACE_ITEMS:
                assert items is not None
                new_revision = _Revision(
                    review.revision + 1,
                    review.hold,
                    _canonical_items(items),
                    command_id,
                    ACTION_REPLACE_ITEMS,
                )
                # 先按“下一版本”渲染并落盘，提交成功后才推进内存状态。
                body = _render_body(
                    self._body_for(review, command_id, revision=new_revision)
                )

                def db_writes() -> None:
                    self._db.execute(
                        "INSERT INTO revisions (review_id, revision, hold, "
                        "items_json, command_id, action, verdict_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        self._revision_params(review_id, new_revision),
                    )

                self._persist_success(
                    command_id, canonical_key, review, 200, body, db_writes
                )
                review.revisions.append(new_revision)
                return 200, body

            # 确认不推进版本号，只冻结当前快照。
            body = _render_body(
                self._body_for(review, command_id, status=CONFIRMED)
            )

            def db_writes() -> None:
                self._db.execute(
                    "UPDATE reviews SET status = ? WHERE review_id = ?",
                    (CONFIRMED, review_id),
                )

            self._persist_success(
                command_id, canonical_key, review, 200, body, db_writes
            )
            review.status = CONFIRMED
            return 200, body


# 进程级单例存储。
store = ReviewStore()
