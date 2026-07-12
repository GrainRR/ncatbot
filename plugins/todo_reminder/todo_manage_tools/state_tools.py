"""Todo 状态变更用途工具。"""

from __future__ import annotations

from typing import Any, Callable

from ..todo_store import STATUS_DELETED, STATUS_DONE, STATUS_OPEN, TodoReminder, TodoStore
from .common import (
    TodoToolContext,
    ToolResult,
    ToolSpec,
    format_inline,
    number_from_args,
    numbers_from_args,
    numbers_schema,
    object_schema,
    resolve_todo,
    status_changed_result,
    target_schema,
    todo_to_dict,
)


def complete_todos(
    store: TodoStore,
    context: TodoToolContext,
    args: dict[str, Any],
) -> ToolResult:
    """完成一个或多个未完成待办。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        args: 已通过 schema 校验的工具参数，包含 `numbers`、`number`
            或 `reference`。

    Returns:
        已完成待办列表；目标不存在或状态非法时返回错误结果。
    """

    numbers = numbers_from_args(args, context)
    items = [resolve_todo(store, context, number, (STATUS_OPEN,), "完成") for number in numbers]
    completed: list[TodoReminder] = []
    for item in items:
        updated = store.complete(item.id)
        if updated is None:
            return status_changed_result(store, context, item.todo_no, "完成")
        completed.append(updated)
    return ToolResult(
        ok=True,
        status="success",
        message="已完成待办：" + "、".join(format_inline(item) for item in completed),
        data={"items": [todo_to_dict(item, context.timezone) for item in completed]},
    )


def cancel_todo(
    store: TodoStore,
    context: TodoToolContext,
    args: dict[str, Any],
) -> ToolResult:
    """取消一条未完成待办。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        args: 已通过 schema 校验的工具参数，包含 `number` 或 `reference`。

    Returns:
        已软删除的待办；目标不存在或状态非法时返回错误结果。
    """

    number = number_from_args(args, context)
    item = resolve_todo(store, context, number, (STATUS_OPEN,), "取消")
    canceled = store.cancel(item.id)
    if canceled is None:
        return status_changed_result(store, context, number, "取消")
    return ToolResult(
        ok=True,
        status="success",
        message=f"已取消待办：{format_inline(canceled)}",
        data={"item": todo_to_dict(canceled, context.timezone)},
    )


def restore_todos(
    store: TodoStore,
    context: TodoToolContext,
    args: dict[str, Any],
) -> ToolResult:
    """恢复一个或多个已完成或已取消待办。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        args: 已通过 schema 校验的工具参数，包含 `numbers`、`number`
            或 `reference`。

    Returns:
        已恢复待办列表；目标不存在或状态非法时返回错误结果。
    """

    numbers = numbers_from_args(args, context)
    items = [
        resolve_todo(store, context, number, (STATUS_DONE, STATUS_DELETED), "恢复")
        for number in numbers
    ]
    restored: list[TodoReminder] = []
    for item in items:
        updated = store.restore(item.id)
        if updated is None:
            return status_changed_result(store, context, item.todo_no, "恢复")
        restored.append(updated)
    return ToolResult(
        ok=True,
        status="success",
        message="已恢复待办：" + "、".join(format_inline(item) for item in restored),
        data={"items": [todo_to_dict(item, context.timezone) for item in restored]},
    )


def delete_todos(
    store: TodoStore,
    context: TodoToolContext,
    args: dict[str, Any],
) -> ToolResult:
    """永久删除一个或多个待办。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        args: 已通过 schema 校验的工具参数，包含目标编号和 `confirmed`。

    Returns:
        未确认时返回确认结果且不删库；确认后返回永久删除结果。
    """

    numbers = numbers_from_args(args, context)
    items = [resolve_todo(store, context, number, None, "永久删除") for number in numbers]
    if args.get("confirmed") is not True:
        return ToolResult(
            ok=False,
            status="confirm",
            message=(
                "永久删除不可恢复。请确认是否永久删除："
                + "、".join(format_inline(item) for item in items)
            ),
            data={"items": [todo_to_dict(item, context.timezone) for item in items], "deleted": False},
        )
    for item in items:
        store.delete_permanent(item.id)
    return ToolResult(
        ok=True,
        status="success",
        message="已永久删除待办：" + "、".join(f"[{item.todo_no}] {item.title}" for item in items),
        data={"items": [todo_to_dict(item, context.timezone) for item in items], "deleted": True},
    )


def build_tool_specs(
    handlers: dict[str, Callable[[dict[str, Any]], ToolResult]],
) -> dict[str, ToolSpec]:
    """构建状态变更用途工具定义。

    Args:
        handlers: 以工具名为键的后端执行函数映射。

    Returns:
        状态变更工具的 ToolSpec 映射。
    """

    return {
        "complete_todos": ToolSpec(
            "complete_todos",
            "完成一个或多个未完成待办。编号必须是用户当前可见编号。",
            numbers_schema(required=[]),
            handlers["complete_todos"],
        ),
        "cancel_todo": ToolSpec(
            "cancel_todo",
            "取消一条未完成待办，这是软删除路径，不需要永久删除确认。",
            target_schema(required=[]),
            handlers["cancel_todo"],
        ),
        "restore_todos": ToolSpec(
            "restore_todos",
            "恢复一个或多个已完成或已取消待办。",
            numbers_schema(required=[]),
            handlers["restore_todos"],
        ),
        "delete_todos": ToolSpec(
            "delete_todos",
            "永久删除一个或多个待办。默认必须确认，confirmed 不为 true 时不能执行删除。",
            object_schema(
                {
                    "numbers": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1},
                    "number": {"type": ["integer", "null"], "minimum": 1},
                    "reference": {"type": ["string", "null"]},
                    "confirmed": {"type": "boolean"},
                },
                required=[],
            ),
            handlers["delete_todos"],
        ),
    }
