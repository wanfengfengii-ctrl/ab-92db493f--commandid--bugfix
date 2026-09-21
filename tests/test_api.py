"""API 判据：结论与依据、稳定错误代码、响应字节级排列不变性。"""

import itertools
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
ASSESS_URL = "/api/v1/stowage/assess"
REMOVAL_IMPACT_URL = "/api/v1/stowage/removal-impact"


def post(payload):
    return client.post(ASSESS_URL, json=payload)


def post_removal_impact(payload):
    return client.post(REMOVAL_IMPACT_URL, json=payload)


def test_forbid_response():
    response = post(
        {
            "hold": "HOLD-3",
            "items": [
                {"id": "C101", "category": "FLAM"},
                {"id": "C205", "category": "OXID"},
                {"id": "C330", "category": "WET"},
            ],
        }
    )
    assert response.status_code == 200
    assert response.json() == {
        "hold": "HOLD-3",
        "conclusion": "FORBID",
        "evidence": [
            {
                "first": "C101",
                "second": "C205",
                "firstCategory": "FLAM",
                "secondCategory": "OXID",
                "rule": "FORBID",
            }
        ],
    }


def test_partition_response():
    response = post(
        {
            "hold": "H1",
            "items": [
                {"id": "B2", "category": "FLAM"},
                {"id": "A1", "category": "TOX"},
            ],
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["conclusion"] == "PARTITION"
    # 依据中的配对按编号先小后大，与录入顺序无关。
    assert [(e["first"], e["second"]) for e in body["evidence"]] == [("A1", "B2")]
    assert body["evidence"][0]["rule"] == "PARTITION"


def test_allow_response():
    response = post(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "GAS"},
                {"id": "B2", "category": "WET"},
            ],
        }
    )
    assert response.status_code == 200
    assert response.json()["conclusion"] == "ALLOW"
    assert response.json()["evidence"] == []


def test_evidence_sorted_by_id_pair_over_api():
    response = post(
        {
            "hold": "H1",
            "items": [
                {"id": "Z9", "category": "TOX"},
                {"id": "A1", "category": "FLAM"},
                {"id": "M5", "category": "OXID"},
            ],
        }
    )
    assert response.status_code == 200
    assert response.json()["conclusion"] == "FORBID"
    assert [
        (e["first"], e["second"]) for e in response.json()["evidence"]
    ] == [("A1", "M5"), ("A1", "Z9"), ("M5", "Z9")]


def test_permutation_invariance_byte_identical():
    items = [
        {"id": "P1", "category": "FLAM"},
        {"id": "P2", "category": "OXID"},
        {"id": "P3", "category": "TOX"},
        {"id": "P4", "category": "WET"},
        {"id": "P5", "category": "CORR"},
        {"id": "P6", "category": "GAS"},
    ]
    bodies = set()
    for perm in itertools.permutations(items):
        response = post({"hold": "H9", "items": list(perm)})
        assert response.status_code == 200
        bodies.add(response.content)
    assert len(bodies) == 1


@pytest.mark.parametrize("hold", ["", "   ", None, 7])
def test_empty_hold_rejected(hold):
    payload = {
        "items": [
            {"id": "A", "category": "FLAM"},
            {"id": "B", "category": "GAS"},
        ]
    }
    if hold is not None:
        payload["hold"] = hold
    response = post(payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EMPTY_HOLD"


@pytest.mark.parametrize("count", [0, 1, 21])
def test_item_count_out_of_range(count):
    items = [{"id": f"I{i:02d}", "category": "WET"} for i in range(count)]
    response = post({"hold": "H1", "items": items})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ITEM_COUNT_OUT_OF_RANGE"


@pytest.mark.parametrize("count", [2, 20])
def test_item_count_bounds_accepted(count):
    items = [{"id": f"I{i:02d}", "category": "WET"} for i in range(count)]
    response = post({"hold": "H1", "items": items})
    assert response.status_code == 200
    assert response.json()["conclusion"] == "ALLOW"


def test_duplicate_item_id_rejected():
    response = post(
        {
            "hold": "H1",
            "items": [
                {"id": "A", "category": "FLAM"},
                {"id": "A", "category": "GAS"},
            ],
        }
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DUPLICATE_ITEM_ID"


@pytest.mark.parametrize("category", ["RADIO", "flam", "", ["FLAM"], 3])
def test_unknown_category_rejected(category):
    response = post(
        {
            "hold": "H1",
            "items": [
                {"id": "A", "category": "FLAM"},
                {"id": "B", "category": category},
            ],
        }
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNKNOWN_CATEGORY"


@pytest.mark.parametrize("item_id", ["", None, 5, ["A"]])
def test_invalid_item_id_rejected(item_id):
    payload = {
        "hold": "H1",
        "items": [
            {"id": "B", "category": "FLAM"},
            {"id": item_id, "category": "GAS"},
        ],
    }
    if item_id is None:
        payload["items"][1] = {"category": "GAS"}
    response = post(payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ITEM_ID"


@pytest.mark.parametrize(
    "payload",
    [
        "not-an-object",
        {"hold": "H1"},
        {"hold": "H1", "items": "not-a-list"},
        {"hold": "H1", "items": [{"id": "A", "category": "FLAM"}, "broken"]},
    ],
    ids=["body-not-object", "items-missing", "items-not-list", "item-not-object"],
)
def test_invalid_request_rejected(payload):
    response = post(payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_malformed_json_rejected():
    response = client.post(
        ASSESS_URL,
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_JSON"


def test_error_body_shape_is_stable():
    response = post({"hold": "", "items": []})
    assert response.status_code == 400
    error = response.json()["error"]
    assert set(error) == {"code", "message"}
    assert error["code"] == "EMPTY_HOLD"
    assert isinstance(error["message"], str) and error["message"]


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_removal_impact_forbid_with_multiple_improvement_levels():
    response = post_removal_impact(
        {
            "hold": "HOLD-3",
            "items": [
                {"id": "A1", "category": "FLAM"},
                {"id": "B2", "category": "GAS"},
                {"id": "C3", "category": "CORR"},
            ],
        }
    )
    assert response.status_code == 200
    assert response.json() == {
        "hold": "HOLD-3",
        "original": {
            "conclusion": "FORBID",
            "evidence": [
                {
                    "first": "A1",
                    "second": "B2",
                    "firstCategory": "FLAM",
                    "secondCategory": "GAS",
                    "rule": "FORBID",
                },
                {
                    "first": "B2",
                    "second": "C3",
                    "firstCategory": "GAS",
                    "secondCategory": "CORR",
                    "rule": "PARTITION",
                },
            ],
        },
        "removals": [
            {
                "removed": "A1",
                "conclusion": "PARTITION",
                "evidence": [
                    {
                        "first": "B2",
                        "second": "C3",
                        "firstCategory": "GAS",
                        "secondCategory": "CORR",
                        "rule": "PARTITION",
                    }
                ],
            },
            {"removed": "B2", "conclusion": "ALLOW", "evidence": []},
            {
                "removed": "C3",
                "conclusion": "FORBID",
                "evidence": [
                    {
                        "first": "A1",
                        "second": "B2",
                        "firstCategory": "FLAM",
                        "secondCategory": "GAS",
                        "rule": "FORBID",
                    }
                ],
            },
        ],
        "recommendations": ["A1", "B2"],
    }


def test_removal_impact_partition_can_drop_to_allow():
    response = post_removal_impact(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "TOX"},
                {"id": "B2", "category": "FLAM"},
                {"id": "C3", "category": "WET"},
            ],
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["original"]["conclusion"] == "PARTITION"
    assert [(r["removed"], r["conclusion"]) for r in body["removals"]] == [
        ("A1", "ALLOW"),
        ("B2", "ALLOW"),
        ("C3", "PARTITION"),
    ]
    assert body["recommendations"] == ["A1", "B2"]


def test_removal_impact_two_items_removal_leaves_single_item_allowed():
    response = post_removal_impact(
        {
            "hold": "H1",
            "items": [
                {"id": "A1", "category": "TOX"},
                {"id": "B2", "category": "FLAM"},
            ],
        }
    )
    assert response.status_code == 200
    body = response.json()
    assert body["original"]["conclusion"] == "PARTITION"
    assert [(r["removed"], r["conclusion"], r["evidence"]) for r in body["removals"]] == [
        ("A1", "ALLOW", []),
        ("B2", "ALLOW", []),
    ]
    assert body["recommendations"] == ["A1", "B2"]


def test_removal_impact_no_improvement_gives_empty_recommendations():
    response = post_removal_impact(
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
    assert response.status_code == 200
    body = response.json()
    assert body["original"]["conclusion"] == "FORBID"
    assert all(r["conclusion"] == "FORBID" for r in body["removals"])
    assert body["recommendations"] == []


def test_removal_impact_original_matches_assess_response():
    payload = {
        "hold": "HOLD-3",
        "items": [
            {"id": "C3", "category": "CORR"},
            {"id": "A1", "category": "FLAM"},
            {"id": "B2", "category": "GAS"},
        ],
    }
    assess_body = post(payload).json()
    impact_body = post_removal_impact(payload).json()
    assert impact_body["hold"] == assess_body["hold"]
    assert impact_body["original"] == {
        "conclusion": assess_body["conclusion"],
        "evidence": assess_body["evidence"],
    }


def test_removal_impact_permutation_invariance_byte_identical():
    items = [
        {"id": "P1", "category": "FLAM"},
        {"id": "P2", "category": "GAS"},
        {"id": "P3", "category": "CORR"},
        {"id": "P4", "category": "TOX"},
        {"id": "P5", "category": "WET"},
    ]
    bodies = set()
    for perm in itertools.permutations(items):
        response = post_removal_impact({"hold": "H9", "items": list(perm)})
        assert response.status_code == 200
        bodies.add(response.content)
    assert len(bodies) == 1


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"hold": "", "items": [{"id": "A", "category": "FLAM"}, {"id": "B", "category": "GAS"}]}, "EMPTY_HOLD"),
        ({"hold": "  ", "items": [{"id": "A", "category": "FLAM"}, {"id": "B", "category": "GAS"}]}, "EMPTY_HOLD"),
        (
            {
                "hold": "H1",
                "items": [{"id": "A", "category": "FLAM"}, {"id": "A", "category": "GAS"}],
            },
            "DUPLICATE_ITEM_ID",
        ),
        (
            {
                "hold": "H1",
                "items": [{"id": "A", "category": "FLAM"}, {"id": "B", "category": "RADIO"}],
            },
            "UNKNOWN_CATEGORY",
        ),
        ({"hold": "H1", "items": [{"id": "A", "category": "FLAM"}]}, "ITEM_COUNT_OUT_OF_RANGE"),
        (
            {"hold": "H1", "items": [{"id": f"I{i:02d}", "category": "WET"} for i in range(21)]},
            "ITEM_COUNT_OUT_OF_RANGE",
        ),
        ({"hold": "H1"}, "INVALID_REQUEST"),
        ("not-an-object", "INVALID_REQUEST"),
    ],
    ids=[
        "empty-hold",
        "blank-hold",
        "duplicate-item-id",
        "unknown-category",
        "too-few-items",
        "too-many-items",
        "items-missing",
        "body-not-object",
    ],
)
def test_removal_impact_invalid_payload_rejected_without_partial_analysis(payload, code):
    response = post_removal_impact(payload)
    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code


def test_removal_impact_malformed_json_rejected():
    response = client.post(
        REMOVAL_IMPACT_URL,
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "INVALID_JSON"
