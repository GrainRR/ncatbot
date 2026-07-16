"""Todo 修改用途工具。"""

from __future__ import annotations

from typing import Any, Callable

from ..todo_store import STATUS_OPEN, TodoStore
from .common import (
    TodoToolContext,
    ToolResult,
    ToolSpec,
    clean_optional_text,
    clean_required_text,
    format_inline,
    number_from_args,
    object_schema,
    parse_optional_time,
    resolve_todo,
    shift_fields,
    status_changed_result,
    time_field_label,
    todo_to_dict,
)


def edit_todo(
    store: TodoStore,
    context: TodoToolContext,
    args: dict[str, Any],
) -> ToolResult:
    """修改一条未完成待办的可编辑字段。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        args: 已通过 schema 校验的工具参数，包含目标编号和要更新的
            标题、内容、时间或提醒文案。

    Returns:
        修改后的待办；没有可更新字段时返回澄清结果。
    """

    number = number_from_args(args, context)
    item = resolve_todo(store, context, number, (STATUS_OPEN,), "修改")
    updates: dict[str, Any] = {}

    if "title" in args and args.get("title") is not None:
        updates["title"] = clean_required_text(args.get("title"), "title")
    if "content" in args:
        updates["content"] = clean_optional_text(args.get("content"))
    if args.get("clear_reminder_at") is True:
        updates["remind_at"] = None
    elif "reminder_at" in args and args.get("reminder_at") is not None:
        updates["remind_at"] = parse_optional_time(args.get("reminder_at"), "reminder_at", context)
    if args.get("clear_due_at") is True:
        updates["due_at"] = None
    elif "due_at" in args and args.get("due_at") is not None:
        updates["due_at"] = parse_optional_time(args.get("due_at"), "due_at", context)
    if "reminder_text" in args and args.get("reminder_text") is not None:
        updates["reminder_text"] = clean_required_text(args.get("reminder_text"), "reminder_text")

    if not updates:
        return ToolResult(
            ok=False,
            status="clarify",
            message="需要补充要修改的标题、内容或时间",
            data={"number": number},
        )

    updated = store.update_fields(
        item.id,
        updates,
        STATUS_OPEN,
        context.user_id,
        context.now,
        context.reject_past_reminder,
    )
    if updated is None:
        return status_changed_result(store, context, number, "修改")
    return ToolResult(
        ok=True,
        status="success",
        message=f"已修改待办：{format_inline(updated)}",
        data={"item": todo_to_dict(updated, context.timezone)},
    )


def shift_todo_time(
    store: TodoStore,
    context: TodoToolContext,
    args: dict[str, Any],
) -> ToolResult:
    """按分钟提前或推迟待办时间。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        args: 已通过 schema 校验的工具参数，包含目标编号、时间字段、
            调整方向和正整数分钟数。

    Returns:
        更新后的待办；字段不明确或目标没有时间字段时返回澄清结果。
    """

    number = number_from_args(args, context)
    item = resolve_todo(store, context, number, (STATUS_OPEN,), "调整时间")
    field = args["field"]
    direction = args["direction"]
    delta_minutes = args["delta_minutes"]
    delta_seconds = int(delta_minutes) * 60
    if direction == "earlier":
        delta_seconds *= -1

    fields = shift_fields(item, field)
    updates: dict[str, Any] = {}
    for field_name in fields:
        current_value = item.remind_at if field_name == "reminder_at" else item.due_at
        if current_value is None:
            continue
        store_field = "remind_at" if field_name == "reminder_at" else "due_at"
        updates[store_field] = current_value + delta_seconds

    if not updates:
        return ToolResult(
            ok=False,
            status="clarify",
            message="这条待办没有任何时间字段，需要用户补充要调整哪个时间",
            data={"number": number},
        )

    updated = store.update_fields(
        item.id,
        updates,
        STATUS_OPEN,
        context.user_id,
        context.now,
        context.reject_past_reminder,
    )
    if updated is None:
        return status_changed_result(store, context, number, "调整时间")
    direction_text = "提前" if direction == "earlier" else "推迟"
    field_text = "、".join(time_field_label(field_name) for field_name in fields)
    return ToolResult(
        ok=True,
        status="success",
        message=(
            f"已将第 {updated.todo_no} 条待办的{field_text}{direction_text} {delta_minutes} 分钟："
            f"{format_inline(updated)}"
        ),
        data={
            "item": todo_to_dict(updated, context.timezone),
            "shifted_fields": fields,
            "delta_minutes": delta_minutes,
            "direction": direction,
        },
    )


def build_tool_specs(
    handlers: dict[str, Callable[[dict[str, Any]], ToolResult]],
) -> dict[str, ToolSpec]:
    """构建修改用途工具定义。

    Args:
        handlers: 以工具名为键的后端执行函数映射。

    Returns:
        修改工具的 ToolSpec 映射。
    """

    return {
        "edit_todo": ToolSpec(
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
            handlers["edit_todo"],
        ),
        "shift_todo_time": ToolSpec(
            "shift_todo_time",
            "把一条待办的提醒时间、截止时间或两者按分钟提前或推迟。时间计算只能由后端执行。",
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
            handlers["shift_todo_time"],
        ),
    }
