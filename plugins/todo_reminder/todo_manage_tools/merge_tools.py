"""Todo 合并用途工具。"""

from __future__ import annotations

from typing import Any, Callable

from ..todo_store import STATUS_OPEN, TodoStore
from .common import (
    TodoToolContext,
    ToolResult,
    ToolSpec,
    clean_optional_text,
    format_inline,
    numbers_from_args,
    object_schema,
    resolve_todo,
    status_changed_result,
    todo_to_dict,
)


def merge_todos(
    store: TodoStore,
    context: TodoToolContext,
    args: dict[str, Any],
) -> ToolResult:
    """合并多条未完成待办。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        args: 已通过 schema 校验的工具参数，包含至少两个用户可见编号
            和可选合并后标题。

    Returns:
        合并后的待办；编号不足或状态非法时返回澄清或错误结果。
    """

    numbers = numbers_from_args(args, context)
    if len(numbers) < 2:
        return ToolResult(
            ok=False,
            status="clarify",
            message="合并待办至少需要两个编号",
            data={"numbers": numbers},
        )
    items = [resolve_todo(store, context, number, (STATUS_OPEN,), "合并") for number in numbers]
    title = clean_optional_text(args.get("title")) or "；".join(item.title for item in items)
    content_parts = [item.content or item.title for item in items]
    remind_values = [item.remind_at for item in items if item.remind_at is not None]
    due_values = [item.due_at for item in items if item.due_at is not None]
    updates = {
        "title": title,
        "content": "\n".join(content_parts),
        "raw_text": context.user_text or "合并待办",
        "reminder_text": "",
        "remind_at": min(remind_values) if remind_values else None,
        "due_at": max(due_values) if due_values else None,
    }
    merged = store.update_fields(items[0].id, updates, STATUS_OPEN)
    if merged is None:
        return status_changed_result(store, context, items[0].todo_no, "合并")
    for item in items[1:]:
        store.cancel(item.id)
    return ToolResult(
        ok=True,
        status="success",
        message=f"已合并为待办：{format_inline(merged)}",
        data={
            "item": todo_to_dict(merged, context.timezone),
            "merged_numbers": numbers,
        },
    )


def build_tool_specs(
    handlers: dict[str, Callable[[dict[str, Any]], ToolResult]],
) -> dict[str, ToolSpec]:
    """构建合并用途工具定义。

    Args:
        handlers: 以工具名为键的后端执行函数映射。

    Returns:
        合并工具的 ToolSpec 映射。
    """

    return {
        "merge_todos": ToolSpec(
            "merge_todos",
            "合并多个未完成待办，保留第一个编号并取消其余编号。",
            object_schema(
                {
                    "numbers": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2},
                    "title": {"type": ["string", "null"]},
                },
                required=["numbers"],
            ),
            handlers["merge_todos"],
        ),
    }
