"""修改工具：edit_todo、shift_todo_time。"""

from __future__ import annotations

from typing import Any

from ..todo_store import STATUS_OPEN, TodoReminder
from .contracts import ToolExecutionStop, ToolResult, ToolRuntime, ToolSpec
from .presentation import format_inline, todo_to_dict
from .targets import number_from_args, resolve_open_todo
from .validation import (
    clean_optional_text,
    clean_required_text,
    object_schema,
    parse_optional_time,
    target_schema,
)


__all__ = ["tool_specs"]


def tool_specs() -> tuple[ToolSpec, ...]:
    """返回修改工具定义。"""

    return (
        ToolSpec(
            "edit_todo",
            "修改一条未完成待办的标题、内容、提醒时间或截止时间。",
            object_schema(
                {
                    "number": {"type": ["integer", "null"], "minimum": 1},
                    "reference": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "content": {"type": ["string", "null"]},
                    "reminder_at": {"type": ["string", "null"]},
                    "due_at": {"type": ["string", "null"]},
                    "reminder_text": {"type": ["string", "null"]},
                    "clear_reminder_at": {"type": "boolean"},
                    "clear_due_at": {"type": "boolean"},
                },
                required=[],
            ),
            _edit_todo,
        ),
        ToolSpec(
            "shift_todo_time",
            "把未完成待办的提醒时间、截止时间或两者按分钟提前或推迟。",
            object_schema(
                {
                    "number": {"type": ["integer", "null"], "minimum": 1},
                    "reference": {"type": ["string", "null"]},
                    "field": {"type": "string", "enum": ["auto", "due_at", "reminder_at", "both"]},
                    "direction": {"type": "string", "enum": ["later", "earlier"]},
                    "delta_minutes": {"type": "integer", "minimum": 1},
                },
                required=["field", "direction", "delta_minutes"],
            ),
            _shift_todo_time,
        ),
    )


def _edit_todo(runtime: ToolRuntime, args: dict[str, Any]) -> ToolResult:
    """修改一条未完成待办的可编辑字段。

    Args:
        runtime: 含当前用户、时区、当前时间及存储实例的可信运行时。
        args: 已通过 Schema 校验的参数。`number`/`reference` 定位未完成待办；
            `clear_reminder_at` 和 `clear_due_at` 的优先级高于对应时间文本。

    Returns:
        修改后的待办；并发导致目标状态变化时返回结构化失败结果。

    Raises:
        ToolExecutionStop: 目标编号缺失、待办非未完成状态，或没有任何可修改字段时。
        ReminderTimeValidationError: 新提醒时间不在未来时，由执行器统一转换。
    """

    context = runtime.context
    number = number_from_args(args, runtime)
    item = resolve_open_todo(runtime, number, "修改")
    updates: dict[str, Any] = {}
    if args.get("title") is not None:
        updates["title"] = clean_required_text(args["title"], "title")
    if "content" in args:
        updates["content"] = clean_optional_text(args.get("content"))
    if args.get("clear_reminder_at") is True:
        updates["remind_at"] = None
    elif args.get("reminder_at") is not None:
        updates["remind_at"] = parse_optional_time(args["reminder_at"], "reminder_at", context)
    if args.get("clear_due_at") is True:
        updates["due_at"] = None
    elif args.get("due_at") is not None:
        updates["due_at"] = parse_optional_time(args["due_at"], "due_at", context)
    if args.get("reminder_text") is not None:
        updates["reminder_text"] = clean_required_text(args["reminder_text"], "reminder_text")
    if not updates:
        raise ToolExecutionStop(
            ToolResult(False, "clarify", "需要补充要修改的标题、内容或时间", {"number": number})
        )
    updated = runtime.store.update_fields(
        item.id,
        updates,
        STATUS_OPEN,
        context.user_id,
        context.now,
        context.reject_past_reminder,
    )
    if updated is None:
        return _status_changed_result(runtime, number, "修改")
    return ToolResult(True, "success", f"已修改待办：{format_inline(updated)}", {"item": todo_to_dict(updated, context.timezone)})


def _shift_todo_time(runtime: ToolRuntime, args: dict[str, Any]) -> ToolResult:
    """按分钟整体调整一条未完成待办的一个或多个时间字段。

    Args:
        runtime: 可信运行时；其中 `now` 用于阻止提前后落入过去的提醒时间。
        args: 已校验的定位、`field`、`direction` 和正整数 `delta_minutes`。
            `field=auto` 只在待办恰有一个时间字段时可消歧。

    Returns:
        含实际调整字段和分钟数的成功结果；并发状态变化时返回失败结果。

    Raises:
        ToolExecutionStop: 目标缺少可调整字段，或 `auto` 无法判断要调整的时间时。
        ReminderTimeValidationError: 向前调整产生当前时间及以前的提醒时间时。
    """

    context = runtime.context
    number = number_from_args(args, runtime)
    item = resolve_open_todo(runtime, number, "调整时间")
    fields = _shift_fields(item, args["field"])
    delta_seconds = int(args["delta_minutes"]) * 60
    if args["direction"] == "earlier":
        delta_seconds *= -1
    updates: dict[str, int] = {}
    for field_name in fields:
        current_value = item.remind_at if field_name == "reminder_at" else item.due_at
        if current_value is not None:
            updates["remind_at" if field_name == "reminder_at" else "due_at"] = current_value + delta_seconds
    if not updates:
        raise ToolExecutionStop(
            ToolResult(False, "clarify", "这条待办没有任何时间字段，需要用户补充要调整哪个时间", {"number": number})
        )
    updated = runtime.store.update_fields(
        item.id,
        updates,
        STATUS_OPEN,
        context.user_id,
        context.now,
        context.reject_past_reminder,
    )
    if updated is None:
        return _status_changed_result(runtime, number, "调整时间")
    direction_text = "提前" if args["direction"] == "earlier" else "推迟"
    field_text = "、".join("提醒时间" if value == "reminder_at" else "截止时间" for value in fields)
    return ToolResult(
        True,
        "success",
        f"已将第 {updated.todo_no} 条待办的{field_text}{direction_text} {args['delta_minutes']} 分钟：{format_inline(updated)}",
        {
            "item": todo_to_dict(updated, context.timezone),
            "shifted_fields": fields,
            "delta_minutes": args["delta_minutes"],
            "direction": args["direction"],
        },
    )


def _shift_fields(item: TodoReminder, field: str) -> list[str]:
    """将工具层字段选择解析为实际可更新的时间字段。

    Args:
        item: 已确认处于 `open` 状态的目标待办。
        field: Schema 限定为 `auto`、`reminder_at`、`due_at` 或 `both` 的字段选择。

    Returns:
        应写入的字段名列表；`both` 始终保留两个字段，即使其中一个值为空。

    Raises:
        ToolExecutionStop: 指定字段不存在、待办没有时间，或 `auto` 对双时间待办不明确时。
    """

    if field == "both":
        if item.remind_at is None and item.due_at is None:
            raise _missing_time_field(item)
        return ["reminder_at", "due_at"]
    if field == "reminder_at":
        if item.remind_at is None:
            raise ToolExecutionStop(ToolResult(False, "clarify", "这条待办没有提醒时间，需要用户补充新的提醒时间", {"number": item.todo_no}))
        return ["reminder_at"]
    if field == "due_at":
        if item.due_at is None:
            raise ToolExecutionStop(ToolResult(False, "clarify", "这条待办没有截止时间，需要用户补充新的截止时间", {"number": item.todo_no}))
        return ["due_at"]
    has_reminder = item.remind_at is not None
    has_due = item.due_at is not None
    if not has_reminder and not has_due:
        raise _missing_time_field(item)
    if has_reminder and has_due:
        raise ToolExecutionStop(
            ToolResult(False, "clarify", "这条待办同时有提醒时间和截止时间，请说明要调整哪一个", {"number": item.todo_no})
        )
    return ["reminder_at"] if has_reminder else ["due_at"]


def _missing_time_field(item: TodoReminder) -> ToolExecutionStop:
    return ToolExecutionStop(
        ToolResult(False, "clarify", "这条待办没有任何时间字段，需要用户补充要调整哪个时间", {"number": item.todo_no})
    )


def _status_changed_result(runtime: ToolRuntime, number: int, action: str) -> ToolResult:
    context = runtime.context
    existing = runtime.store.find_by_no(context.scope, context.group_id, context.user_id, number, None)
    if existing is not None:
        return ToolResult(False, "error", f"第 {number} 条待办当前状态已变化，不能{action}", {"number": number, "status": existing.status})
    return ToolResult(False, "error", f"找不到第 {number} 条待办，请先查看待办列表确认编号", {"number": number})
