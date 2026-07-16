"""稳定历史 ID、未完成编号与状态目标解析。"""

from __future__ import annotations

from typing import Any, Sequence

from ..todo_store import TodoReminder
from .contracts import ToolExecutionStop, ToolResult, ToolRuntime
from .presentation import status_label
from .validation import clean_optional_text


REFERENCE_WORDS = {
    "刚才那个",
    "刚才那条",
    "刚刚那个",
    "刚刚那条",
    "上一个",
    "上一条",
    "前一个",
    "前一条",
}


def number_from_args(args: dict[str, Any], runtime: ToolRuntime) -> int:
    """解析当前未完成待办的用户可见序号。"""

    number = args.get("number")
    if number is not None:
        return int(number)
    reference = clean_optional_text(args.get("reference"))
    if reference in REFERENCE_WORDS and runtime.context.last_todo_no is not None:
        return int(runtime.context.last_todo_no)
    if reference and reference.isdigit():
        return int(reference)
    raise ToolExecutionStop(
        ToolResult(False, "clarify", "请说明要操作第几条待办", {"reference": reference})
    )


def numbers_from_args(args: dict[str, Any], runtime: ToolRuntime) -> list[int]:
    """解析并去重多个当前未完成待办序号。"""

    numbers = args.get("numbers")
    if numbers:
        values = list(dict.fromkeys(int(number) for number in numbers))
        if values:
            return values
    return [number_from_args(args, runtime)]


def history_ids_from_args(args: dict[str, Any]) -> list[str]:
    """解析并去重稳定历史 ID。"""

    values = args.get("history_ids")
    if values:
        history_ids = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        if history_ids:
            return history_ids
    history_id = clean_optional_text(args.get("history_id"))
    if history_id:
        return [history_id]
    raise ToolExecutionStop(
        ToolResult(False, "clarify", "请提供待办列表中显示的历史 ID（例如 H-...）", {})
    )


def resolve_open_todo(
    runtime: ToolRuntime,
    number: int,
    action: str,
) -> TodoReminder:
    """解析当前未完成待办。"""

    return resolve_todo(runtime, number, ("open",), action)


def resolve_todo(
    runtime: ToolRuntime,
    number: int,
    statuses: Sequence[str] | None,
    action: str,
) -> TodoReminder:
    """按可复用序号查找未完成路径使用的待办。"""

    context = runtime.context
    item = runtime.store.find_by_no(
        context.scope, context.group_id, context.user_id, number, statuses
    )
    if item is not None:
        return item
    existing = runtime.store.find_by_no(
        context.scope, context.group_id, context.user_id, number, None
    )
    if existing is not None:
        raise ToolExecutionStop(
            ToolResult(
                False,
                "error",
                f"第 {number} 条待办当前状态是{status_label(existing.status)}，不能{action}",
                {"number": number, "status": existing.status},
            )
        )
    raise ToolExecutionStop(
        ToolResult(False, "error", f"找不到第 {number} 条待办，请先查看待办列表确认编号", {"number": number})
    )


def resolve_history_todo(
    runtime: ToolRuntime,
    history_id: str,
    statuses: Sequence[str] | None,
    action: str,
) -> TodoReminder:
    """按不可复用历史 ID 解析恢复或永久删除目标。"""

    item = runtime.store.find_by_history_id(runtime.context.user_id, history_id, statuses)
    if item is not None:
        return item
    existing = runtime.store.find_by_history_id(runtime.context.user_id, history_id, None)
    if existing is not None:
        raise ToolExecutionStop(
            ToolResult(
                False,
                "error",
                f"历史 ID {history_id} 的待办当前状态是{status_label(existing.status)}，不能{action}",
                {"history_id": history_id, "status": existing.status},
            )
        )
    raise ToolExecutionStop(
        ToolResult(False, "error", f"找不到历史 ID {history_id} 对应的待办，请先查看待办列表", {"history_id": history_id})
    )


def batch_state_changed_result(action: str, target_field: str, targets: list[Any]) -> ToolResult:
    """表示事务内复核失败，且没有任何部分写入。"""

    return ToolResult(
        False,
        "error",
        f"待办状态或归属已变化，批量{action}未执行，数据没有部分修改",
        {target_field: targets, "atomic": True},
    )
