"""交接审核判据：草稿/命令/确认、判重重放、版本冲突与并发原子性。"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
REVIEWS_URL = "/api/v1/stowage/reviews"


def create_review(payload):
    return client.post(REVIEWS_URL, json=payload)


def send_command(review_id, payload):
    return client.post(f"{REVIEWS_URL}/{review_id}/commands", json=payload)


def forbid_payload(command_id, hold="HOLD-3"):
    return {
        "hold": hold,
        "commandId": command_id,
        "items": [
            {"id": "C330", "category": "WET"},
            {"id": "C101", "category": "FLAM"},
            {"id": "C205", "category": "OXID"},
        ],
    }


# ---- 建草稿 --------------------------------------------------------------


def test_create_review_starts_draft_at_revision_one():
    response = create_review(forbid_payload("cmd-create-1"))
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["revision"] == 1
    assert body["commandId"] == "cmd-create-1"
    assert body["hold"] == "HOLD-3"
    # 规范化：货项按编号排序，裁决复用规则引擎。
    assert [item["id"] for item in body["items"]] == ["C101", "C205", "C330"]
    assert body["conclusion"] == "FORBID"
    assert body["evidence"] == [
        {
            "first": "C101",
            "second": "C205",
            "firstCategory": "FLAM",
            "secondCategory": "OXID",
            "rule": "FORBID",
        }
    ]
    assert isinstance(body["reviewId"], str) and body["reviewId"]


def test_create_review_replay_is_byte_identical():
    first = create_review(forbid_payload("cmd-create-replay"))
    # 同 commandId、货项录入次序不同：规范化后内容相同，原样重放。
    reordered = forbid_payload("cmd-create-replay")
    reordered["items"] = list(reversed(reordered["items"]))
    second = create_review(reordered)
    assert second.status_code == 201
    assert second.content == first.content


def test_create_review_command_id_reused_with_different_content():
    create_review(forbid_payload("cmd-create-collision"))
    other = forbid_payload("cmd-create-collision")
    other["items"][0]["category"] = "GAS"
    response = create_review(other)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMAND_ID_REUSED"


def test_create_review_reuses_assess_validation():
    payload = forbid_payload("cmd-create-invalid")
    payload["items"][1]["category"] = "RADIO"
    response = create_review(payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNKNOWN_CATEGORY"


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda p: p.pop("commandId"), "INVALID_COMMAND_ID"),
        (lambda p: p.update(commandId="   "), "INVALID_COMMAND_ID"),
        (lambda p: p.update(commandId=9), "INVALID_COMMAND_ID"),
        (lambda p: p.update(hold=""), "EMPTY_HOLD"),
        (lambda p: p.update(items=[{"id": "A", "category": "FLAM"}]),
         "ITEM_COUNT_OUT_OF_RANGE"),
    ],
)
def test_create_review_invalid_payload_rejected_without_draft(mutate, code):
    payload = forbid_payload(f"cmd-create-bad-{code}")
    mutate(payload)
    response = create_review(payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


# ---- 替换货项 ------------------------------------------------------------


def test_replace_items_advances_revision_and_recomputes_verdict():
    review_id = create_review(forbid_payload("cmd-replace-1")).json()["reviewId"]

    response = send_command(
        review_id,
        {
            "commandId": "cmd-replace-2",
            "action": "REPLACE_ITEMS",
            "expectedRevision": 1,
            "items": [
                {"id": "B2", "category": "FLAM"},
                {"id": "A1", "category": "TOX"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 2
    assert body["status"] == "DRAFT"
    assert body["commandId"] == "cmd-replace-2"
    assert [item["id"] for item in body["items"]] == ["A1", "B2"]
    assert body["conclusion"] == "PARTITION"
    assert len(body["evidence"]) == 1


def test_replace_items_replay_stays_byte_identical_after_further_revisions():
    review_id = create_review(forbid_payload("cmd-rr-1")).json()["reviewId"]

    replace_body = {
        "commandId": "cmd-rr-2",
        "action": "REPLACE_ITEMS",
        "expectedRevision": 1,
        "items": [
            {"id": "A1", "category": "GAS"},
            {"id": "B2", "category": "WET"},
        ],
    }
    first = send_command(review_id, replace_body)
    assert first.status_code == 200
    assert first.json()["revision"] == 2

    # 另一个命令把版本推进到 3。
    advanced = send_command(
        review_id,
        {
            "commandId": "cmd-rr-3",
            "action": "REPLACE_ITEMS",
            "expectedRevision": 2,
            "items": [
                {"id": "A1", "category": "TOX"},
                {"id": "B2", "category": "FLAM"},
            ],
        },
    )
    assert advanced.status_code == 200
    assert advanced.json()["revision"] == 3

    # 版本推进后重放旧命令（expectedRevision 仍为 1）：字节必须与首次一致，
    # 而不是按当前版本报冲突。
    replay = send_command(review_id, replace_body)
    assert replay.status_code == 200
    assert replay.content == first.content
    assert replay.json()["revision"] == 2


def test_replace_items_validation_reuses_assess_rules():
    review_id = create_review(forbid_payload("cmd-rv-1")).json()["reviewId"]
    response = send_command(
        review_id,
        {
            "commandId": "cmd-rv-bad",
            "action": "REPLACE_ITEMS",
            "expectedRevision": 1,
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "A1", "category": "GAS"},
            ],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DUPLICATE_ITEM_ID"
    # 失败的命令不留部分状态：版本仍为 1。
    assert send_command(
        review_id,
        {
            "commandId": "cmd-rv-check",
            "action": "CONFIRM",
            "expectedRevision": 1,
        },
    ).status_code == 200


# ---- 版本冲突与重试 ------------------------------------------------------


def test_revision_conflict_then_retry_on_new_revision_succeeds():
    review_id = create_review(forbid_payload("cmd-rc-1")).json()["reviewId"]
    # 先合法推进到版本 2。
    assert send_command(
        review_id,
        {
            "commandId": "cmd-rc-2",
            "action": "REPLACE_ITEMS",
            "expectedRevision": 1,
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "WET"},
            ],
        },
    ).status_code == 200

    # 旧版本号的命令失败，且同一 commandId 可在修正版本号后重试。
    stale = {
        "commandId": "cmd-rc-retry",
        "action": "REPLACE_ITEMS",
        "expectedRevision": 1,
        "items": [
            {"id": "A1", "category": "TOX"},
            {"id": "B2", "category": "FLAM"},
        ],
    }
    conflict = send_command(review_id, stale)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "REVISION_CONFLICT"

    stale["expectedRevision"] = 2
    retried = send_command(review_id, stale)
    assert retried.status_code == 200
    assert retried.json()["revision"] == 3


def test_review_not_found_for_unknown_review():
    response = send_command(
        "does-not-exist",
        {
            "commandId": "cmd-404",
            "action": "CONFIRM",
            "expectedRevision": 1,
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_new_command_error_ordering_not_found_beats_conflict_and_finalized():
    response = send_command(
        "missing-review",
        {
            "commandId": "cmd-order-404",
            "action": "CONFIRM",
            "expectedRevision": 99,
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REVIEW_NOT_FOUND"


def test_command_id_dedup_precedes_review_lookup():
    # commandId 已在审核 A 上成功；把它发给不存在的审核 → 仍先判重，
    # 因内容（review_id 不同）不一致得到 COMMAND_ID_REUSED 而非 404。
    review_id = create_review(forbid_payload("cmd-dedup-create")).json()["reviewId"]
    assert send_command(
        review_id,
        {"commandId": "cmd-dedup-confirm", "action": "CONFIRM", "expectedRevision": 1},
    ).status_code == 200
    response = send_command(
        "another-missing-review",
        {"commandId": "cmd-dedup-confirm", "action": "CONFIRM", "expectedRevision": 1},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMAND_ID_REUSED"


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"action": "CONFIRM", "expectedRevision": 1}, "INVALID_COMMAND_ID"),
        ({"commandId": "c", "action": "FREEZE", "expectedRevision": 1}, "INVALID_ACTION"),
        ({"commandId": "c", "action": "CONFIRM"}, "INVALID_REVISION"),
        ({"commandId": "c", "action": "CONFIRM", "expectedRevision": 0}, "INVALID_REVISION"),
        ({"commandId": "c", "action": "CONFIRM", "expectedRevision": True}, "INVALID_REVISION"),
        ({"commandId": "c", "action": "CONFIRM", "expectedRevision": "1"}, "INVALID_REVISION"),
    ],
)
def test_command_invalid_payload_rejected(payload, code):
    review_id = create_review(forbid_payload(f"cmd-shape-{code}")).json()["reviewId"]
    response = send_command(review_id, payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


def test_command_malformed_json_rejected():
    review_id = create_review(forbid_payload("cmd-malformed")).json()["reviewId"]
    response = client.post(
        f"{REVIEWS_URL}/{review_id}/commands",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


# ---- 确认与冻结 ----------------------------------------------------------


def test_confirm_freezes_snapshot_without_advancing_revision():
    review_id = create_review(forbid_payload("cmd-confirm-1")).json()["reviewId"]
    confirm = {
        "commandId": "cmd-confirm-2",
        "action": "CONFIRM",
        "expectedRevision": 1,
    }
    first = send_command(review_id, confirm)
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "CONFIRMED"
    assert body["revision"] == 1
    assert body["conclusion"] == "FORBID"

    # 重试确认：字节一致。
    replay = send_command(review_id, confirm)
    assert replay.status_code == 200
    assert replay.content == first.content


def test_confirmed_review_replaces_and_conflicts_report_revision_conflict_first():
    review_id = create_review(forbid_payload("cmd-final-1")).json()["reviewId"]
    assert send_command(
        review_id,
        {"commandId": "cmd-final-2", "action": "CONFIRM", "expectedRevision": 1},
    ).status_code == 200

    # 已冻结：版本号正确的任何新命令 → REVIEW_FINALIZED。
    response = send_command(
        review_id,
        {"commandId": "cmd-final-3", "action": "CONFIRM", "expectedRevision": 1},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVIEW_FINALIZED"

    response = send_command(
        review_id,
        {
            "commandId": "cmd-final-4",
            "action": "REPLACE_ITEMS",
            "expectedRevision": 1,
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "WET"},
            ],
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVIEW_FINALIZED"

    # 报错顺序：版本不对时先报 REVISION_CONFLICT，再论冻结。
    response = send_command(
        review_id,
        {"commandId": "cmd-final-5", "action": "CONFIRM", "expectedRevision": 2},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REVISION_CONFLICT"


# ---- 并发原子性（真实 HTTP 服务器）---------------------------------------


@pytest.fixture(scope="module")
def live_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import time
    import urllib.request

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1
            ) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("test server failed to start")

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _http_post(base_url: str, path: str, payload: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_concurrent_commands_on_same_revision_only_one_commits(live_server):
    status, body = _http_post(
        live_server, REVIEWS_URL, forbid_payload("cmd-race-create")
    )
    assert status == 201
    review_id = json.loads(body)["reviewId"]

    winner = {}
    results: list[tuple[int, str, str]] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        if index == 0:
            payload = {
                "commandId": f"cmd-race-{index}",
                "action": "CONFIRM",
                "expectedRevision": 1,
            }
        else:
            payload = {
                "commandId": f"cmd-race-{index}",
                "action": "REPLACE_ITEMS",
                "expectedRevision": 1,
                "items": [
                    {"id": f"A{index}", "category": "TOX"},
                    {"id": "B0", "category": "FLAM"},
                ],
            }
        barrier.wait()
        code, raw = _http_post(
            live_server, f"{REVIEWS_URL}/{review_id}/commands", payload
        )
        parsed = json.loads(raw)
        error_code = parsed.get("error", {}).get("code", "")
        results.append((code, error_code, payload["commandId"]))
        if code == 200:
            winner.setdefault("payload", payload)
            winner.setdefault("body", parsed)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [result for result in results if result[0] == 200]
    assert len(successes) == 1
    failures = [result for result in results if result[0] != 200]
    assert all(result[0] == 409 for result in failures)

    if winner["body"]["status"] == "CONFIRMED":
        # 确认先赢：版本仍为 1 但已冻结，其余命令通过版本检查后得到
        # REVIEW_FINALIZED（报错顺序：REVISION_CONFLICT 先于 FINALIZED）。
        assert all(result[1] == "REVIEW_FINALIZED" for result in failures)
        assert winner["body"]["revision"] == 1
        status, raw = _http_post(
            live_server,
            f"{REVIEWS_URL}/{review_id}/commands",
            {"commandId": "cmd-race-after", "action": "CONFIRM", "expectedRevision": 1},
        )
        assert status == 409
        assert json.loads(raw)["error"]["code"] == "REVIEW_FINALIZED"
    else:
        # 替换先赢：只有一个版本被提交（revision=2），其余命令版本过期。
        assert all(result[1] == "REVISION_CONFLICT" for result in failures)
        assert winner["body"]["revision"] == 2
        loser_payload = {
            "commandId": "cmd-race-retry",
            "action": "REPLACE_ITEMS",
            "expectedRevision": 2,
            "items": [
                {"id": "X1", "category": "GAS"},
                {"id": "Y2", "category": "WET"},
            ],
        }
        status, raw = _http_post(
            live_server, f"{REVIEWS_URL}/{review_id}/commands", loser_payload
        )
        assert status == 200
        assert json.loads(raw)["revision"] == 3

        # 获胜命令重放：仍是首次的 revision=2 字节。
        status, raw = _http_post(
            live_server,
            f"{REVIEWS_URL}/{review_id}/commands",
            winner["payload"],
        )
        assert status == 200
        assert json.loads(raw)["revision"] == 2
