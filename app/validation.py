"""请求体校验：任何非法输入整体拒绝，并返回稳定错误代码。"""

from __future__ import annotations

from app.rules import CATEGORIES

MIN_ITEMS = 2
MAX_ITEMS = 20

# 预审命令类型。
ACTION_REPLACE_ITEMS = "REPLACE_ITEMS"
ACTION_CONFIRM = "CONFIRM"
COMMAND_ACTIONS = frozenset({ACTION_REPLACE_ITEMS, ACTION_CONFIRM})


class ApiError(Exception):
    """携带稳定错误代码与 HTTP 状态码的校验错误。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _validate_hold(payload: dict) -> str:
    hold = payload.get("hold")
    if not isinstance(hold, str) or not hold.strip():
        raise ApiError("EMPTY_HOLD", "Field 'hold' must be a non-empty string.")
    return hold


def _validate_items(raw_items: object) -> list[tuple[str, str]]:
    """校验 ``items`` 数组，返回 ``[(编号, 类别), ...]``。

    校验顺序固定：数组类型 → 数量 → 逐项（编号 → 类别 → 编号唯一性），
    保证同类非法输入永远得到同一个错误代码。
    """
    if not isinstance(raw_items, list):
        raise ApiError("INVALID_REQUEST", "Field 'items' must be a JSON array.")

    count = len(raw_items)
    if not MIN_ITEMS <= count <= MAX_ITEMS:
        raise ApiError(
            "ITEM_COUNT_OUT_OF_RANGE",
            f"Field 'items' must contain between {MIN_ITEMS} and {MAX_ITEMS} "
            f"entries; got {count}.",
        )

    seen_ids: set[str] = set()
    items: list[tuple[str, str]] = []
    for index, entry in enumerate(raw_items):
        if not isinstance(entry, dict):
            raise ApiError(
                "INVALID_REQUEST", f"Item at index {index} must be a JSON object."
            )
        item_id = entry.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ApiError(
                "INVALID_ITEM_ID",
                f"Item at index {index} must have a non-empty string 'id'.",
            )
        category = entry.get("category")
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ApiError(
                "UNKNOWN_CATEGORY",
                f"Item '{item_id}' has unknown category {category!r}; "
                f"expected one of {sorted(CATEGORIES)}.",
            )
        if item_id in seen_ids:
            raise ApiError(
                "DUPLICATE_ITEM_ID", f"Duplicate item id '{item_id}'."
            )
        seen_ids.add(item_id)
        items.append((item_id, category))

    return items


def validate_payload(payload: object) -> tuple[str, list[tuple[str, str]]]:
    """校验并解析请求体，返回 ``(舱位, [(编号, 类别), ...])``。

    校验顺序固定：整体结构 → 舱位 → 货项数量 → 逐项（编号 → 类别 →
    编号唯一性），保证同类非法输入永远得到同一个错误代码。
    """
    if not isinstance(payload, dict):
        raise ApiError("INVALID_REQUEST", "Request body must be a JSON object.")

    hold = _validate_hold(payload)
    items = _validate_items(payload.get("items"))
    return hold, items


def _validate_command_id(payload: dict) -> str:
    command_id = payload.get("commandId")
    if not isinstance(command_id, str) or not command_id.strip():
        raise ApiError(
            "INVALID_COMMAND_ID",
            "Field 'commandId' must be a non-empty string.",
        )
    return command_id


def validate_review_create_payload(
    payload: object,
) -> tuple[str, list[tuple[str, str]], str]:
    """校验建草稿请求，返回 ``(舱位, [(编号, 类别), ...], commandId)``。

    复用 ``assess`` 的全部货项校验，非法输入整体拒绝、不产生草稿。
    """
    if not isinstance(payload, dict):
        raise ApiError("INVALID_REQUEST", "Request body must be a JSON object.")

    hold = _validate_hold(payload)
    items = _validate_items(payload.get("items"))
    command_id = _validate_command_id(payload)
    return hold, items, command_id


def validate_review_command_payload(
    payload: object,
) -> tuple[str, str, int, list[tuple[str, str]] | None]:
    """校验草稿命令，返回 ``(commandId, action, expectedRevision, items)``。

    ``items`` 仅在 ``REPLACE_ITEMS`` 时存在并经过与 ``assess`` 完全相同的
    校验；``CONFIRM`` 时为 ``None``。
    """
    if not isinstance(payload, dict):
        raise ApiError("INVALID_REQUEST", "Request body must be a JSON object.")

    command_id = _validate_command_id(payload)

    action = payload.get("action")
    if action not in COMMAND_ACTIONS:
        raise ApiError(
            "INVALID_ACTION",
            f"Field 'action' must be one of {sorted(COMMAND_ACTIONS)}; "
            f"got {action!r}.",
        )

    expected_revision = payload.get("expectedRevision")
    # bool 是 int 的子类，需显式排除（True/False 不是合法版本号）。
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise ApiError(
            "INVALID_REVISION",
            "Field 'expectedRevision' must be a positive integer.",
        )

    items: list[tuple[str, str]] | None = None
    if action == ACTION_REPLACE_ITEMS:
        items = _validate_items(payload.get("items"))

    return command_id, action, expected_revision, items
