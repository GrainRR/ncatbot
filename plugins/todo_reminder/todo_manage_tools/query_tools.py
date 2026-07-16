"""查询工具：list_todos、get_todo。"""

from __future__ import annotations

from typing import Any

from ..todo_store import STATUS_DELETED, STATUS_DONE, STATUS_OPEN
from .contracts import ToolResult, ToolRuntime, ToolSpec
from .presentation import format_detail, format_list, todo_to_dict
from .targets import number_from_args, resolve_todo
from .validation import object_schema, target_schema


__all__ = ["tool_specs"]


def tool_specs() -> tuple[ToolSpec, ...]:
    """返回查询工具定义。"""

    return (
        ToolSpec(
            "list_todos",
            "列出当前用户的待办。查询请求优先由程序处理，此工具用于复杂上下文。",
            object_schema(
                {
                    "status": {
                        "type": "string",
                        "enum": [STATUS_OPEN, STATUS_DONE, STATUS_DELETED, "all"],
                    },
                    "limit": {"type": "integer", "minimum": 1},
                },
                required=[],
            ),
            _list_todos,
        ),
        ToolSpec(
            "get_todo",
            "按当前未完成待办序号查看详情；不能传数据库内部 id。",
            target_schema(required=[]),
            _get_todo,
        ),
    )


def _list_todos(runtime: ToolRuntime, args: dict[str, Any]) -> ToolResult:
    status = args.get("status") or STATUS_OPEN
    limit = args.get("limit") or 20
    store_status = None if status == "all" else status
    context = runtime.context
    items = runtime.store.list_by_status(
        context.scope, context.group_id, context.user_id, store_status, limit
    )
    title = {
        STATUS_OPEN: "待办列表",
        STATUS_DONE: "已完成待办",
        STATUS_DELETED: "已取消待办",
        "all": "全部待办",
    }.get(status, "待办列表")
    return ToolResult(
        True,
        "success",
        format_list(title, items, context.timezone),
        {"items": [todo_to_dict(item, context.timezone) for item in items]},
    )


def _get_todo(runtime: ToolRuntime, args: dict[str, Any]) -> ToolResult:
    number = number_from_args(args, runtime)
    item = resolve_todo(runtime, number, None, "查看")
    return ToolResult(
        True,
        "success",
        format_detail(item, runtime.context.timezone),
        {"item": todo_to_dict(item, runtime.context.timezone)},
    )
