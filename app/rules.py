"""危险品隔离规则引擎与同舱裁决。

规则为无向配对：类别组合命中禁止表则 FORBID，命中隔板表则 PARTITION，
其余（含同类）一律 ALLOW。舱位级结论按 FORBID > PARTITION > ALLOW 归并。
"""

from __future__ import annotations

ALLOW = "ALLOW"
PARTITION = "PARTITION"
FORBID = "FORBID"

# 本服务受理的危险品类别子集，除此之外的类别一律拒绝。
CATEGORIES = frozenset({"FLAM", "OXID", "TOX", "CORR", "WET", "GAS"})

# 禁止同舱的无向类别对。
_FORBID_PAIRS = frozenset(
    frozenset(pair)
    for pair in (("FLAM", "OXID"), ("FLAM", "GAS"), ("WET", "CORR"))
)

# 必须加隔板的无向类别对。
_PARTITION_PAIRS = frozenset(
    frozenset(pair)
    for pair in (("TOX", "FLAM"), ("TOX", "OXID"), ("CORR", "GAS"))
)


def classify_pair(category_a: str, category_b: str) -> str:
    """裁决一个无向类别对。

    规则表按集合匹配，两个类别谁先谁后不影响结果；同类配对与
    未出现在规则表中的配对均为 ALLOW。
    """
    key = frozenset((category_a, category_b))
    if key in _FORBID_PAIRS:
        return FORBID
    if key in _PARTITION_PAIRS:
        return PARTITION
    return ALLOW


def assess(items: list[tuple[str, str]]) -> dict:
    """对一舱货项做同舱裁决。

    ``items`` 为 ``(货项编号, 类别)`` 列表，编号必须唯一。返回
    ``{"conclusion": ..., "evidence": [...]}``：枚举全部无向货项对，
    命中规则的对进入依据列表；每对内部编号先小后大，列表整体按
    （首编号, 次编号） 升序。结论按 FORBID > PARTITION > ALLOW 归并。
    """
    ordered = sorted(items, key=lambda item: item[0])
    evidence: list[dict] = []
    for left in range(len(ordered)):
        for right in range(left + 1, len(ordered)):
            first_id, first_category = ordered[left]
            second_id, second_category = ordered[right]
            rule = classify_pair(first_category, second_category)
            if rule == ALLOW:
                continue
            evidence.append(
                {
                    "first": first_id,
                    "second": second_id,
                    "firstCategory": first_category,
                    "secondCategory": second_category,
                    "rule": rule,
                }
            )

    rules_hit = {entry["rule"] for entry in evidence}
    if FORBID in rules_hit:
        conclusion = FORBID
    elif PARTITION in rules_hit:
        conclusion = PARTITION
    else:
        conclusion = ALLOW
    return {"conclusion": conclusion, "evidence": evidence}


# 结论严重程度，用于判断移除一件货后是否改善。
_SEVERITY = {ALLOW: 0, PARTITION: 1, FORBID: 2}


def removal_impact(items: list[tuple[str, str]]) -> dict:
    """单件移除影响分析：逐一卸下每件货后重新裁决同一舱位。

    ``items`` 为 ``(货项编号, 类别)`` 列表，编号必须唯一。返回
    ``{"original": ..., "removals": [...], "recommendations": [...]}``：

    - ``original``：未移除任何货项时的原始裁决（与 ``assess`` 相同结构）；
    - ``removals``：按货项编号升序，每条记录卸下该货后的结论与依据；
      移除后只剩一件货时没有配对，按 ALLOW 处理；
    - ``recommendations``：能把严重程度降级的货项编号（禁止→隔板/允许、
      隔板→允许），按字典序排列。

    分析完全复用 ``assess``，且 ``assess`` 内部按编号排序，因此录入顺序
    不影响输出。
    """
    original = assess(items)
    original_severity = _SEVERITY[original["conclusion"]]

    removals: list[dict] = []
    recommendations: list[str] = []
    for removed_id, _category in sorted(items, key=lambda item: item[0]):
        remaining = [item for item in items if item[0] != removed_id]
        outcome = assess(remaining)
        removals.append(
            {
                "removed": removed_id,
                "conclusion": outcome["conclusion"],
                "evidence": outcome["evidence"],
            }
        )
        if _SEVERITY[outcome["conclusion"]] < original_severity:
            recommendations.append(removed_id)

    return {
        "original": original,
        "removals": removals,
        "recommendations": sorted(recommendations),
    }
