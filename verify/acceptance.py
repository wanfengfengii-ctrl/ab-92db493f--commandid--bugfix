"""一次性验收：对运行中的 API 实例做 HTTP 级验收检查。

通过环境变量 API_BASE_URL 指向被验实例（默认 http://api:8000，
即 Compose 网络内的 api 服务）。全部检查通过退出码为 0，否则为 1。
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("API_BASE_URL", "http://api:8000").rstrip("/")
ASSESS_URL = f"{BASE_URL}/api/v1/stowage/assess"
REMOVAL_IMPACT_URL = f"{BASE_URL}/api/v1/stowage/removal-impact"
REVIEWS_URL = f"{BASE_URL}/api/v1/stowage/reviews"
HEALTH_URL = f"{BASE_URL}/health"

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        if detail:
            print(f"       {detail}")
        _failures.append(name)


def wait_for_api(timeout_seconds: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(1.0)
    return False


def post_assess(payload: dict) -> tuple[int, bytes]:
    return _post(ASSESS_URL, payload)


def post_removal_impact(payload: dict) -> tuple[int, bytes]:
    return _post(REMOVAL_IMPACT_URL, payload)


def _post(url: str, payload: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def expect_error(name: str, payload: dict, code: str, url: str = ASSESS_URL) -> None:
    status, body = _post(url, payload)
    parsed = json.loads(body)
    check(
        name,
        status == 400
        and set(parsed) == {"error"}
        and parsed.get("error", {}).get("code") == code,
        f"status={status} body={body!r}",
    )


def main() -> int:
    # 内存存储在服务进程内持续存在；为使脚本可对同一实例重复运行，
    # 所有 commandId 加上本次运行唯一前缀，避免与历史命令判重冲突。
    rid = uuid.uuid4().hex[:8]
    check("api healthy", wait_for_api(), f"no /health at {BASE_URL}")
    if _failures:
        return 1

    status, body = post_assess(
        {
            "hold": "HOLD-3",
            "items": [
                {"id": "C101", "category": "FLAM"},
                {"id": "C205", "category": "OXID"},
                {"id": "C330", "category": "WET"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "forbid pair yields FORBID with evidence",
        status == 200
        and data.get("conclusion") == "FORBID"
        and data.get("evidence")
        == [
            {
                "first": "C101",
                "second": "C205",
                "firstCategory": "FLAM",
                "secondCategory": "OXID",
                "rule": "FORBID",
            }
        ],
        f"status={status} body={body!r}",
    )

    status, body = post_assess(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "TOX"},
                {"id": "B2", "category": "FLAM"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "partition pair yields PARTITION",
        status == 200
        and data.get("conclusion") == "PARTITION"
        and len(data.get("evidence", [])) == 1,
        f"status={status} body={body!r}",
    )

    status, body = post_assess(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "WET"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "rule-free pairs yield ALLOW with empty evidence",
        status == 200
        and data.get("conclusion") == "ALLOW"
        and data.get("evidence") == [],
        f"status={status} body={body!r}",
    )

    items = [
        {"id": "P1", "category": "FLAM"},
        {"id": "P2", "category": "OXID"},
        {"id": "P3", "category": "TOX"},
        {"id": "P4", "category": "WET"},
        {"id": "P5", "category": "CORR"},
    ]
    bodies = set()
    for perm in itertools.permutations(items):
        status, body = post_assess({"hold": "H9", "items": list(perm)})
        bodies.add(body if status == 200 else f"status={status}".encode())
    check(
        "permuted submissions are byte-identical",
        len(bodies) == 1,
        f"{len(bodies)} distinct response bodies across {len(list(itertools.permutations(items)))} permutations",
    )

    expect_error(
        "duplicate item id rejected",
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "A1", "category": "GAS"},
            ],
        },
        "DUPLICATE_ITEM_ID",
    )
    expect_error(
        "unknown category rejected",
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "RADIO"},
            ],
        },
        "UNKNOWN_CATEGORY",
    )
    expect_error(
        "item count out of range rejected",
        {"hold": "H1", "items": [{"id": "A1", "category": "FLAM"}]},
        "ITEM_COUNT_OUT_OF_RANGE",
    )
    expect_error(
        "empty hold rejected",
        {
            "hold": "  ",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "GAS"},
            ],
        },
        "EMPTY_HOLD",
    )

    # ---- 单件移除影响分析（removal-impact）----

    status, body = post_removal_impact(
        {
            "hold": "HOLD-3",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "GAS"},
                {"id": "C3", "category": "CORR"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "removal-impact: forbid scenario offers multiple improvement levels",
        status == 200
        and data.get("original", {}).get("conclusion") == "FORBID"
        and [(r.get("removed"), r.get("conclusion")) for r in data.get("removals", [])]
        == [("A1", "PARTITION"), ("B2", "ALLOW"), ("C3", "FORBID")]
        and data.get("recommendations") == ["A1", "B2"],
        f"status={status} body={body!r}",
    )

    status, body = post_removal_impact(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "TOX"},
                {"id": "B2", "category": "FLAM"},
                {"id": "C3", "category": "WET"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "removal-impact: partition scenario can drop to allow",
        status == 200
        and data.get("original", {}).get("conclusion") == "PARTITION"
        and [(r.get("removed"), r.get("conclusion")) for r in data.get("removals", [])]
        == [("A1", "ALLOW"), ("B2", "ALLOW"), ("C3", "PARTITION")]
        and data.get("recommendations") == ["A1", "B2"],
        f"status={status} body={body!r}",
    )

    status, body = post_removal_impact(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "OXID"},
                {"id": "C3", "category": "WET"},
                {"id": "D4", "category": "CORR"},
            ],
        }
    )
    data = json.loads(body)
    check(
        "removal-impact: no improvement yields empty recommendations",
        status == 200
        and data.get("original", {}).get("conclusion") == "FORBID"
        and all(r.get("conclusion") == "FORBID" for r in data.get("removals", []))
        and data.get("recommendations") == [],
        f"status={status} body={body!r}",
    )

    items = [
        {"id": "P1", "category": "FLAM"},
        {"id": "P2", "category": "GAS"},
        {"id": "P3", "category": "CORR"},
        {"id": "P4", "category": "TOX"},
        {"id": "P5", "category": "WET"},
    ]
    bodies = set()
    for perm in itertools.permutations(items):
        status, body = post_removal_impact({"hold": "H9", "items": list(perm)})
        bodies.add(body if status == 200 else f"status={status}".encode())
    check(
        "removal-impact: permuted submissions are byte-identical",
        len(bodies) == 1,
        f"{len(bodies)} distinct response bodies across {len(list(itertools.permutations(items)))} permutations",
    )

    expect_error(
        "removal-impact: duplicate item id rejected",
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "A1", "category": "GAS"},
            ],
        },
        "DUPLICATE_ITEM_ID",
        url=REMOVAL_IMPACT_URL,
    )
    expect_error(
        "removal-impact: unknown category rejected",
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "RADIO"},
            ],
        },
        "UNKNOWN_CATEGORY",
        url=REMOVAL_IMPACT_URL,
    )
    expect_error(
        "removal-impact: item count out of range rejected",
        {"hold": "H1", "items": [{"id": "A1", "category": "FLAM"}]},
        "ITEM_COUNT_OUT_OF_RANGE",
        url=REMOVAL_IMPACT_URL,
    )
    expect_error(
        "removal-impact: empty hold rejected",
        {
            "hold": "  ",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "GAS"},
            ],
        },
        "EMPTY_HOLD",
        url=REMOVAL_IMPACT_URL,
    )

    request = urllib.request.Request(
        REMOVAL_IMPACT_URL,
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    parsed = json.loads(body)
    check(
        "removal-impact: malformed json rejected",
        status == 400
        and set(parsed) == {"error"}
        and parsed.get("error", {}).get("code") == "INVALID_JSON",
        f"status={status} body={body!r}",
    )

    # ---- 交接审核（reviews：草稿 → 命令 → 确认）----

    status, body = _post(
        REVIEWS_URL,
        {
            "hold": "HOLD-3",
            "commandId": f"acc-{rid}-create-1",
            "items": [
                {"id": "C330", "category": "WET"},
                {"id": "C101", "category": "FLAM"},
                {"id": "C205", "category": "OXID"},
            ],
        },
    )
    data = json.loads(body)
    review_id = data.get("reviewId")
    check(
        "reviews: create starts DRAFT at revision 1 with verdict",
        status == 201
        and data.get("status") == "DRAFT"
        and data.get("revision") == 1
        and [item["id"] for item in data.get("items", [])] == ["C101", "C205", "C330"]
        and data.get("conclusion") == "FORBID"
        and isinstance(review_id, str)
        and review_id,
        f"status={status} body={body!r}",
    )

    # 同 commandId 原样重放：字节级一致。
    status2, body2 = _post(
        REVIEWS_URL,
        {
            "hold": "HOLD-3",
            "commandId": f"acc-{rid}-create-1",
            "items": [
                {"id": "C101", "category": "FLAM"},
                {"id": "C205", "category": "OXID"},
                {"id": "C330", "category": "WET"},
            ],
        },
    )
    check(
        "reviews: create replay is byte-identical",
        status2 == 201 and body2 == body,
        f"status={status2} replay={body2!r} original={body!r}",
    )

    # 同 commandId 不同内容 → 409 COMMAND_ID_REUSED。
    status, body = _post(
        REVIEWS_URL,
        {
            "hold": "HOLD-3",
            "commandId": f"acc-{rid}-create-1",
            "items": [
                {"id": "C101", "category": "GAS"},
                {"id": "C205", "category": "OXID"},
            ],
        },
    )
    check(
        "reviews: reused commandId with different content rejected",
        status == 409 and json.loads(body).get("error", {}).get("code")
        == "COMMAND_ID_REUSED",
        f"status={status} body={body!r}",
    )

    # 建草稿复用 assess 校验。
    status, body = _post(
        REVIEWS_URL,
        {
            "hold": "HOLD-3",
            "commandId": f"acc-{rid}-create-bad",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "RADIO"},
            ],
        },
    )
    check(
        "reviews: create reuses assess validation",
        status == 400
        and json.loads(body).get("error", {}).get("code") == "UNKNOWN_CATEGORY",
        f"status={status} body={body!r}",
    )

    commands_url = f"{REVIEWS_URL}/{review_id}/commands"

    # 未知审核优先报 REVIEW_NOT_FOUND。
    status, body = _post(
        f"{REVIEWS_URL}/missing-review/commands",
        {"commandId": f"acc-{rid}-404", "action": "CONFIRM", "expectedRevision": 99},
    )
    check(
        "reviews: unknown review reports REVIEW_NOT_FOUND",
        status == 404
        and json.loads(body).get("error", {}).get("code") == "REVIEW_NOT_FOUND",
        f"status={status} body={body!r}",
    )

    # 旧版本号 → REVISION_CONFLICT，失败命令不占 commandId。
    replace_payload = {
        "commandId": f"acc-{rid}-replace-1",
        "action": "REPLACE_ITEMS",
        "expectedRevision": 9,
        "items": [
            {"id": "B2", "category": "FLAM"},
            {"id": "A1", "category": "TOX"},
        ],
    }
    status, body = _post(commands_url, replace_payload)
    check(
        "reviews: stale expectedRevision reports REVISION_CONFLICT",
        status == 409
        and json.loads(body).get("error", {}).get("code") == "REVISION_CONFLICT",
        f"status={status} body={body!r}",
    )

    # 同一 commandId 修正版本号后重试成功，版本推进到 2。
    replace_payload["expectedRevision"] = 1
    status, body = _post(commands_url, replace_payload)
    data = json.loads(body)
    check(
        "reviews: conflict command retried on current revision advances to 2",
        status == 200
        and data.get("revision") == 2
        and data.get("status") == "DRAFT"
        and data.get("conclusion") == "PARTITION"
        and [item["id"] for item in data.get("items", [])] == ["A1", "B2"],
        f"status={status} body={body!r}",
    )
    first_replace_body = body

    # 版本推进后重放旧命令：仍字节一致。
    status, body = _post(commands_url, replace_payload)
    check(
        "reviews: replace replay is byte-identical after revision advance",
        status == 200 and body == first_replace_body,
        f"status={status} replay={body!r} original={first_replace_body!r}",
    )

    # 确认：冻结快照、不推进版本；重试字节一致。
    confirm_payload = {
        "commandId": f"acc-{rid}-confirm-1",
        "action": "CONFIRM",
        "expectedRevision": 2,
    }
    status, body = _post(commands_url, confirm_payload)
    data = json.loads(body)
    check(
        "reviews: confirm freezes snapshot without advancing revision",
        status == 200
        and data.get("status") == "CONFIRMED"
        and data.get("revision") == 2
        and data.get("conclusion") == "PARTITION",
        f"status={status} body={body!r}",
    )
    status2, body2 = _post(commands_url, confirm_payload)
    check(
        "reviews: confirm replay is byte-identical",
        status2 == 200 and body2 == body,
        f"status={status2} replay={body2!r} original={body!r}",
    )

    # 已冻结审核上的任何新命令 → REVIEW_FINALIZED。
    status, body = _post(
        commands_url,
        {"commandId": f"acc-{rid}-after-final", "action": "CONFIRM", "expectedRevision": 2},
    )
    check(
        "reviews: command on finalized review rejected",
        status == 409
        and json.loads(body).get("error", {}).get("code") == "REVIEW_FINALIZED",
        f"status={status} body={body!r}",
    )

    # 并发：同一版本的替换与确认只有一个原子成功。再建一个草稿做并发实验。
    status, body = _post(
        REVIEWS_URL,
        {
            "hold": "HOLD-3",
            "commandId": f"acc-{rid}-race-create",
            "items": [
                {"id": "C330", "category": "WET"},
                {"id": "C101", "category": "FLAM"},
                {"id": "C205", "category": "OXID"},
            ],
        },
    )
    race_review_id = json.loads(body)["reviewId"]
    race_url = f"{REVIEWS_URL}/{race_review_id}/commands"
    race_results: list[tuple[int, str]] = []
    race_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def race_worker(index: int) -> None:
        if index == 0:
            payload = {
                "commandId": f"acc-{rid}-race-{index}",
                "action": "CONFIRM",
                "expectedRevision": 1,
            }
        else:
            payload = {
                "commandId": f"acc-{rid}-race-{index}",
                "action": "REPLACE_ITEMS",
                "expectedRevision": 1,
                "items": [
                    {"id": f"A{index}", "category": "TOX"},
                    {"id": "B0", "category": "FLAM"},
                ],
            }
        barrier.wait()
        code, raw = _post(race_url, payload)
        with race_lock:
            race_results.append(
                (code, json.loads(raw).get("error", {}).get("code", "OK"))
            )

    threads = [threading.Thread(target=race_worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    successes = [result for result in race_results if result[0] == 200]
    failures = [result for result in race_results if result[0] != 200]
    check(
        "reviews: concurrent commands commit exactly one result",
        len(successes) == 1
        and len(failures) == 7
        and all(code == 409 for code, _ in failures)
        and {error for _, error in failures} <= {"REVISION_CONFLICT", "REVIEW_FINALIZED"},
        f"results={sorted(race_results)}",
    )

    if _failures:
        print(f"\n{len(_failures)} acceptance check(s) failed")
        return 1
    print("\nAll acceptance checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
