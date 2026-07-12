"""Todo 查询用途工具。"""

from __future__ import annotations

from typing import Any, Callable

from ..todo_store import STATUS_DELETED, STATUS_DONE, STATUS_OPEN, TodoStore
from .common import (
    TodoToolContext,
    ToolResult,
    ToolSpec,
    format_detail,
    format_list,
    object_schema,
    resolve_todo,
    target_schema,
    todo_to_dict,
    number_from_args,
)


def list_todos(
    store: TodoStore,
    context: TodoToolContext,
    args: dict[str, Any],
) -> ToolResult:
    """列出当前可信上下文范围内的待办。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        args: 已通过 schema 校验的工具参数，支持 `status` 和 `limit`。

    Returns:
        当前用户当前范围内的待办列表结果。
    """

    status = args.get("status") or STATUS_OPEN
    limit = args.get("limit") or 20
    store_status = None if status == "all" else status
    items = store.list_by_status(
        context.scope,
        context.group_id,
        context.user_id,
        store_status,
        limit,
    )
    title = {
        STATUS_OPEN: "待办列表",
        STATUS_DONE: "已完成待办",
        STATUS_DELETED: "已取消待办",
        "all": "全部待办",
    }.get(status, "待办列表")
    return ToolResult(
        ok=True,
        status="success",
        message=format_list(title, items, context.timezone),
        data={"items": [todo_to_dict(item, context.timezone) for item in items]},
    )


def get_todo(
    store: TodoStore,
    context: TodoToolContext,
    args: dict[str, Any],
) -> ToolResult:
    """按用户可见编号查看待办详情。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        args: 已通过 schema 校验的工具参数，包含 `number` 或 `reference`。

    Returns:
        匹配待办的详情结果。
    """

    number = number_from_args(args, context)
    item = resolve_todo(store, context, number, None, "查看")
    return ToolResult(
        ok=True,
        status="success",
        message=format_detail(item, context.timezone),
        data={"item": todo_to_dict(item, context.timezone)},
    )


def build_tool_specs(
    handlers: dict[str, Callable[[dict[str, Any]], ToolResult]],
) -> dict[str, ToolSpec]:
    """构建查询用途工具定义。

    Args:
        handlers: 以工具名为键的后端执行函数映射。

    Returns:
        查询工具的 ToolSpec 映射。
    """

    return {
        "list_todos": ToolSpec(
            "list_todos",
            "列出当前用户当前会话范围内的待办。查询类请求优先由程序直接处理，本工具仅供复杂上下文使用。",
            object_schema(
                {
                    "status": {"type": "string", "enum": [STATUS_OPEN, STATUS_DONE, STATUS_DELETED, "all"]},
                    "limit": {"type": "integer", "minimum": 1},
                },
                required=[],
            ),
            handlers["list_todos"],
        ),
        "get_todo": ToolSpec(
            "get_todo",
            "按用户可见编号查看一条待办详情。只能传 number 或上下文 reference，不能传数据库 id。",
            target_schema(required=[]),
            handlers["get_todo"],
        ),
    }
