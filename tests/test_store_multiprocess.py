"""跨工作进程语义的回归判据。

``uvicorn --workers N`` 部署下每个工作进程持有独立的 ``ReviewStore``：各自的
SQLite 连接与进程内锁互不共享，只共享同一个数据库文件。这里在同一进程内用
两个独立的 ``ReviewStore`` 实例（不同连接、不同锁、同一文件）精确复现这一
拓扑，验证：

- 同一 commandId 的并发建草稿/命令只产生一次提交，落败方重放首次成功的状态
  码与响应字节（而不是 500）；
- 同一 commandId 配合不同内容并发提交时，恰好一个成功、另一个得到
  409 COMMAND_ID_REUSED；
- 多个不同 commandId 争用同一版本时只有一个原子成功，其余 409；
- 新构造的存储实例（等价于进程/容器重建）从磁盘完整恢复审核与命令记录。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.reviews import ReviewStore
from app.validation import ApiError

ITEMS = [
    ("C330", "WET"),
    ("C101", "FLAM"),
    ("C205", "OXID"),
]


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "stowage.db")


def _make_pair(db_path) -> tuple[ReviewStore, ReviewStore]:
    # 两个实例 = 两个“工作进程”：独立连接、独立进程内锁、共享同一数据库文件。
    return ReviewStore(db_path), ReviewStore(db_path)


def _raises(error: ApiError, code: str) -> bool:
    return isinstance(error, ApiError) and error.code == code


# ---- 建草稿：跨连接判重与原子重放 ----------------------------------------


def test_cross_connection_concurrent_create_replays_identical_bytes(db_path):
    store_a, store_b = _make_pair(db_path)
    barrier = threading.Barrier(2)
    results: list[tuple[int, bytes]] = []

    def worker(store: ReviewStore) -> None:
        barrier.wait()
        results.append(store.create_review("HOLD-3", ITEMS, "cmd-x-create"))

    with ThreadPoolExecutor(2) as pool:
        futs = [pool.submit(worker, store_a), pool.submit(worker, store_b)]
        for fut in futs:
            fut.result()

    assert len(results) == 2
    assert all(code == 201 for code, _ in results)
    first, second = results
    assert first == second  # 状态码与响应字节完全一致
    assert first[1] == second[1]

    # 后到的第三“进程”同样重放首次结果；不同内容则 409。
    store_c = ReviewStore(db_path)
    replay = store_c.create_review("HOLD-3", list(reversed(ITEMS)), "cmd-x-create")
    assert replay == first
    with pytest.raises(ApiError) as exc:
        store_c.create_review("HOLD-OTHER", ITEMS, "cmd-x-create")
    assert exc.value.status == 409
    assert exc.value.code == "COMMAND_ID_REUSED"


def test_cross_connection_divergent_create_yields_one_success_one_conflict(db_path):
    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def worker(index: int) -> None:
        # 每轮使用全新的一对实例，保证双方缓存都为空，只靠 SQLite 仲裁。
        store = ReviewStore(db_path) if index == 0 else ReviewStore(db_path)
        hold = f"HOLD-{index}"
        barrier.wait()
        try:
            outcomes.append(store.create_review(hold, ITEMS, "cmd-x-diverge"))
        except ApiError as error:
            outcomes.append(error)

    with ThreadPoolExecutor(2) as pool:
        futs = [pool.submit(worker, i) for i in range(2)]
        for fut in futs:
            fut.result()

    successes = [o for o in outcomes if isinstance(o, tuple)]
    conflicts = [o for o in outcomes if isinstance(o, ApiError)]
    assert len(successes) == 1
    assert successes[0][0] == 201
    assert len(conflicts) == 1
    assert _raises(conflicts[0], "COMMAND_ID_REUSED")
    assert conflicts[0].status == 409


def test_repeated_cross_connection_create_race_never_errors(db_path):
    # 多轮对冲：每轮一对全新实例并发同一 commandId，任何一轮都不得出现
    # IntegrityError 泄漏（旧实现下落败方会抛 500）。
    for round_index in range(20):
        store_a, store_b = ReviewStore(db_path), ReviewStore(db_path)
        command_id = f"cmd-repeat-{round_index}"
        barrier = threading.Barrier(2)
        results: list[tuple[int, bytes]] = []

        def worker(store: ReviewStore) -> None:
            barrier.wait()
            results.append(store.create_review("HOLD-3", ITEMS, command_id))

        with ThreadPoolExecutor(2) as pool:
            futs = [pool.submit(worker, store_a), pool.submit(worker, store_b)]
            for fut in futs:
                fut.result()

        assert [code for code, _ in results] == [201, 201]
        assert results[0][1] == results[1][1]


# ---- 命令：跨连接版本争用 -------------------------------------------------


def test_cross_connection_command_race_only_one_commits(db_path):
    store_a, store_b = _make_pair(db_path)
    status, body = store_a.create_review("HOLD-3", ITEMS, "cmd-race-create")
    assert status == 201
    review_id = json.loads(body)["reviewId"]
    barrier = threading.Barrier(8)
    outcomes: list[object] = []

    def worker(index: int) -> None:
        store = store_a if index % 2 == 0 else store_b
        payload_items = [(f"A{index}", "TOX"), ("B0", "FLAM")]
        barrier.wait()
        try:
            outcomes.append(
                store.apply_command(
                    review_id,
                    f"cmd-race-{index}",
                    "REPLACE_ITEMS",
                    1,
                    payload_items,
                )
            )
        except ApiError as error:
            outcomes.append(error)

    with ThreadPoolExecutor(8) as pool:
        futs = [pool.submit(worker, i) for i in range(8)]
        for fut in futs:
            fut.result()

    successes = [o for o in outcomes if isinstance(o, tuple)]
    conflicts = [o for o in outcomes if isinstance(o, ApiError)]
    assert len(successes) == 1
    assert successes[0][0] == 200
    assert json.loads(successes[0][1])["revision"] == 2
    assert len(conflicts) == 7
    assert all(_raises(error, "REVISION_CONFLICT") for error in conflicts)

    # 两个“进程”随后都只承认 revision 2：旧版本号命令报冲突，新版本号可确认。
    for store in (store_a, store_b):
        with pytest.raises(ApiError) as stale:
            store.apply_command(review_id, f"cmd-stale-{id(store)}", "CONFIRM", 1, None)
        assert stale.value.code == "REVISION_CONFLICT"
    status, confirmed = store_b.apply_command(
        review_id, "cmd-race-confirm", "CONFIRM", 2, None
    )
    assert status == 200
    assert json.loads(confirmed)["status"] == "CONFIRMED"


def test_cross_connection_same_command_identical_replace_all_replay(db_path):
    store_a, store_b = _make_pair(db_path)
    _, create_body = store_a.create_review("HOLD-3", ITEMS, "cmd-same-create")
    review_id = json.loads(create_body)["reviewId"]
    items = [("B2", "FLAM"), ("A1", "TOX")]  # 录入次序与规范化不同
    barrier = threading.Barrier(4)
    results: list[tuple[int, bytes]] = []

    def worker(store: ReviewStore) -> None:
        barrier.wait()
        results.append(
            store.apply_command(
                review_id, "cmd-same-replace", "REPLACE_ITEMS", 1, items
            )
        )

    with ThreadPoolExecutor(4) as pool:
        stores = [store_a, store_b, store_a, store_b]
        futs = [pool.submit(worker, store) for store in stores]
        for fut in futs:
            fut.result()

    assert [code for code, _ in results] == [200] * 4
    assert len({body for _, body in results}) == 1
    assert json.loads(results[0][1])["revision"] == 2


# ---- 恢复：进程/容器重建后从磁盘完整载入 ----------------------------------


def test_new_store_instance_recovers_full_state_and_replay_bytes(db_path):
    store = ReviewStore(db_path)
    _, create_body = store.create_review("HOLD-3", ITEMS, "cmd-rec-create")
    review_id = json.loads(create_body)["reviewId"]
    replace_items = [("A1", "TOX"), ("B2", "FLAM")]
    _, replace_body = store.apply_command(
        review_id, "cmd-rec-replace", "REPLACE_ITEMS", 1, replace_items
    )
    _, confirm_body = store.apply_command(
        review_id, "cmd-rec-confirm", "CONFIRM", 2, None
    )

    # 全新实例等价于进程/容器重建：不依赖任何进程内缓存，仍恢复全部语义。
    restarted = ReviewStore(db_path)
    assert restarted.create_review("HOLD-3", list(reversed(ITEMS)), "cmd-rec-create") == (
        201,
        create_body,
    )
    assert restarted.apply_command(
        review_id, "cmd-rec-replace", "REPLACE_ITEMS", 1, list(reversed(replace_items))
    ) == (200, replace_body)
    assert restarted.apply_command(
        review_id, "cmd-rec-confirm", "CONFIRM", 2, None
    ) == (200, confirm_body)

    # 已冻结状态随之恢复；报错顺序不变。
    with pytest.raises(ApiError) as finalized:
        restarted.apply_command(review_id, "cmd-rec-new", "CONFIRM", 2, None)
    assert finalized.value.code == "REVIEW_FINALIZED"
    with pytest.raises(ApiError) as conflict:
        restarted.apply_command(review_id, "cmd-rec-wrong", "CONFIRM", 9, None)
    assert conflict.value.code == "REVISION_CONFLICT"
    with pytest.raises(ApiError) as missing:
        restarted.apply_command("no-such-review", "cmd-rec-404", "CONFIRM", 1, None)
    assert missing.value.code == "REVIEW_NOT_FOUND"
