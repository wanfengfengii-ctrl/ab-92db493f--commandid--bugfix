"""规则引擎判据：逐条配对、规则优先级、依据排序与排列不变性。"""

import itertools

import pytest

from app.rules import (
    ALLOW,
    CATEGORIES,
    FORBID,
    PARTITION,
    assess,
    classify_pair,
    removal_impact,
)

_FORBID = {frozenset(p) for p in (("FLAM", "OXID"), ("FLAM", "GAS"), ("WET", "CORR"))}
_PARTITION = {
    frozenset(p) for p in (("TOX", "FLAM"), ("TOX", "OXID"), ("CORR", "GAS"))
}

# 6 个类别的全部无向配对（含同类），共 C(6,2)+6 = 21 种。
ALL_CATEGORY_PAIRS = list(itertools.combinations_with_replacement(sorted(CATEGORIES), 2))


def expected_rule(category_a: str, category_b: str) -> str:
    pair = frozenset((category_a, category_b))
    if pair in _FORBID:
        return FORBID
    if pair in _PARTITION:
        return PARTITION
    return ALLOW


def test_pair_table_covers_every_combination():
    assert len(ALL_CATEGORY_PAIRS) == 21
    assert len(set(ALL_CATEGORY_PAIRS)) == 21


@pytest.mark.parametrize("category_a,category_b", ALL_CATEGORY_PAIRS)
def test_every_pair_outcome(category_a, category_b):
    assert classify_pair(category_a, category_b) == expected_rule(
        category_a, category_b
    )


@pytest.mark.parametrize("category_a,category_b", ALL_CATEGORY_PAIRS)
def test_rules_are_order_independent(category_a, category_b):
    assert classify_pair(category_a, category_b) == classify_pair(
        category_b, category_a
    )


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_same_category_is_allowed(category):
    assert classify_pair(category, category) == ALLOW


def test_forbid_dominates_partition():
    # FLAM–OXID 禁止；TOX–FLAM、TOX–OXID 隔板 → 整舱 FORBID。
    result = assess([("A", "FLAM"), ("B", "OXID"), ("C", "TOX")])
    assert result["conclusion"] == FORBID
    assert [entry["rule"] for entry in result["evidence"]] == [
        FORBID,
        PARTITION,
        PARTITION,
    ]


def test_partition_when_no_forbid_pair():
    # TOX–FLAM 隔板，其余配对允许 → 整舱 PARTITION。
    result = assess([("A", "TOX"), ("B", "FLAM"), ("C", "WET")])
    assert result["conclusion"] == PARTITION
    assert [(e["first"], e["second"], e["rule"]) for e in result["evidence"]] == [
        ("A", "B", PARTITION)
    ]


def test_allow_when_no_rule_hit():
    result = assess([("A", "GAS"), ("B", "OXID"), ("C", "WET")])
    assert result["conclusion"] == ALLOW
    assert result["evidence"] == []


def test_evidence_pairs_oriented_and_sorted():
    # 录入顺序打乱，依据仍须按编号先小后大、按（首, 次）升序。
    result = assess([("Z9", "TOX"), ("A1", "FLAM"), ("M5", "OXID")])
    assert [(e["first"], e["second"]) for e in result["evidence"]] == [
        ("A1", "M5"),
        ("A1", "Z9"),
        ("M5", "Z9"),
    ]
    first = result["evidence"][0]
    assert (first["firstCategory"], first["secondCategory"], first["rule"]) == (
        "FLAM",
        "OXID",
        FORBID,
    )


def test_evidence_lists_only_rule_hits():
    # GAS–WET 允许，不进入依据；WET–CORR 禁止，CORR–GAS 隔板。
    result = assess([("A", "GAS"), ("B", "WET"), ("C", "CORR")])
    assert [(e["first"], e["second"]) for e in result["evidence"]] == [
        ("A", "C"),
        ("B", "C"),
    ]


def test_assess_permutation_invariant():
    items = [
        ("A", "FLAM"),
        ("B", "OXID"),
        ("C", "TOX"),
        ("D", "WET"),
        ("E", "CORR"),
        ("F", "GAS"),
    ]
    baseline = assess(items)
    assert baseline["conclusion"] == FORBID
    for perm in itertools.permutations(items):
        assert assess(list(perm)) == baseline


def test_removal_impact_original_matches_assess():
    items = [("A1", "FLAM"), ("B2", "GAS"), ("C3", "CORR")]
    assert removal_impact(items)["original"] == assess(items)


def test_removal_impact_forbid_with_multiple_improvement_levels():
    # FLAM–GAS 禁止、CORR–GAS 隔板：卸下 FLAM 降为隔板，卸下 GAS 直接允许，
    # 卸下 CORR 仍为禁止 —— 同一禁止场景存在两种改善幅度。
    items = [("A1", "FLAM"), ("B2", "GAS"), ("C3", "CORR")]
    result = removal_impact(items)

    assert result["original"]["conclusion"] == FORBID
    assert [(entry["removed"], entry["conclusion"]) for entry in result["removals"]] == [
        ("A1", PARTITION),
        ("B2", ALLOW),
        ("C3", FORBID),
    ]
    assert result["recommendations"] == ["A1", "B2"]


def test_removal_impact_removals_sorted_by_id_ascending():
    # 录入顺序打乱，removals 仍按货项编号升序。
    items = [("C3", "CORR"), ("A1", "FLAM"), ("B2", "GAS")]
    result = removal_impact(items)
    assert [entry["removed"] for entry in result["removals"]] == ["A1", "B2", "C3"]


def test_removal_impact_removal_entries_carry_evidence():
    items = [("A1", "FLAM"), ("B2", "GAS"), ("C3", "CORR")]
    result = removal_impact(items)
    by_removed = {entry["removed"]: entry for entry in result["removals"]}
    assert by_removed["A1"]["evidence"] == [
        {
            "first": "B2",
            "second": "C3",
            "firstCategory": "GAS",
            "secondCategory": "CORR",
            "rule": PARTITION,
        }
    ]
    assert by_removed["B2"]["evidence"] == []
    assert by_removed["C3"]["evidence"] == [
        {
            "first": "A1",
            "second": "B2",
            "firstCategory": "FLAM",
            "secondCategory": "GAS",
            "rule": FORBID,
        }
    ]


def test_removal_impact_partition_can_drop_to_allow():
    # TOX–FLAM 隔板：卸下 TOX 或 FLAM 均可降为允许，卸下无关的 WET 无改善。
    items = [("A1", "TOX"), ("B2", "FLAM"), ("C3", "WET")]
    result = removal_impact(items)

    assert result["original"]["conclusion"] == PARTITION
    assert [(entry["removed"], entry["conclusion"]) for entry in result["removals"]] == [
        ("A1", ALLOW),
        ("B2", ALLOW),
        ("C3", PARTITION),
    ]
    assert result["recommendations"] == ["A1", "B2"]


def test_removal_impact_two_items_removal_leaves_single_item_allowed():
    # 两件货移除一件后只剩一件：没有配对，按允许处理。
    items = [("A1", "TOX"), ("B2", "FLAM")]
    result = removal_impact(items)

    assert result["original"]["conclusion"] == PARTITION
    assert [(entry["removed"], entry["conclusion"], entry["evidence"]) for entry in result["removals"]] == [
        ("A1", ALLOW, []),
        ("B2", ALLOW, []),
    ]
    assert result["recommendations"] == ["A1", "B2"]


def test_removal_impact_no_improvement_when_already_allow():
    items = [("A1", "GAS"), ("B2", "WET")]
    result = removal_impact(items)

    assert result["original"]["conclusion"] == ALLOW
    assert all(entry["conclusion"] == ALLOW for entry in result["removals"])
    assert result["recommendations"] == []


def test_removal_impact_no_improvement_when_forbid_pairs_independent():
    # 两对互不相干的禁止配对：卸下任何一件都还剩另一对，推荐列表为空。
    items = [("A1", "FLAM"), ("B2", "OXID"), ("C3", "WET"), ("D4", "CORR")]
    result = removal_impact(items)

    assert result["original"]["conclusion"] == FORBID
    assert [(entry["removed"], entry["conclusion"]) for entry in result["removals"]] == [
        ("A1", FORBID),
        ("B2", FORBID),
        ("C3", FORBID),
        ("D4", FORBID),
    ]
    assert result["recommendations"] == []


def test_removal_impact_recommendations_lexicographic():
    # 推荐编号按字典序；字典序与录入顺序无关。
    items = [("B2", "GAS"), ("A1", "FLAM"), ("C3", "CORR")]
    result = removal_impact(items)
    assert result["recommendations"] == sorted(result["recommendations"])


def test_removal_impact_permutation_invariant():
    items = [
        ("A1", "FLAM"),
        ("B2", "GAS"),
        ("C3", "CORR"),
        ("D4", "TOX"),
        ("E5", "WET"),
    ]
    baseline = removal_impact(items)
    assert baseline["original"]["conclusion"] == FORBID
    for perm in itertools.permutations(items):
        assert removal_impact(list(perm)) == baseline
