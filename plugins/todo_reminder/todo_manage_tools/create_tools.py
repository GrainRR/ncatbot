"""Todo 新增用途工具。"""

from __future__ import annotations

from typing import Any, Callable

from ..todo_store import TodoReminderDraft, TodoStore
from .common import (
    TodoToolContext,
    ToolResult,
    ToolSpec,
    clean_optional_text,
    clean_required_text,
    fallback_reminder_text,
    format_inline,
    format_time,
    object_schema,
    parse_optional_time,
    todo_to_dict,
)


def create_todo(
    store: TodoStore,
    context: TodoToolContext,
    args: dict[str, Any],
) -> ToolResult:
    """创建一条待办。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        args: 已通过 schema 校验的工具参数，包含标题、内容、时间和
            可选提醒文案。

    Returns:
        创建成功时返回新待办；参数或业务规则不满足时返回错误结果。
    """

    title = clean_required_text(args.get("title"), "title")
    content = clean_optional_text(args.get("content"))
    raw_text = clean_optional_text(args.get("raw_text")) or context.user_text or title
    reminder_at = parse_optional_time(args.get("reminder_at"), "reminder_at", context)
    due_at = parse_optional_time(args.get("due_at"), "due_at", context)
    reminder_text = clean_optional_text(args.get("reminder_text"))
    if context.reminder_mode == "catgirl" and not reminder_text:
        return ToolResult(
            ok=False,
            status="error",
            message="猫娘模式创建待办需要生成提醒文案，待办没有写入",
            data={},
        )
    if not reminder_text:
        reminder_text = fallback_reminder_text(title)

    if context.reject_past_reminder and reminder_at is not None:
        if reminder_at <= context.now:
            return ToolResult(
                ok=False,
                status="error",
                message="提醒时间已经过去，待办没有写入",
                data={},
            )
    if store.count_pending(
        context.scope,
        context.group_id,
        context.user_id,
    ) >= context.max_pending:
        return ToolResult(
            ok=False,
            status="error",
            message=f"未完成待办已经达到上限 {context.max_pending} 条，请先完成或取消一些待办",
            data={},
        )

    drafts = [
        TodoReminderDraft(
            title=title,
            content=content,
            raw_text=raw_text,
            remind_at=reminder_at,
            due_at=due_at,
            reminder_text=reminder_text,
            llm_json={"tool": "create_todo", "arguments": args},
        )
    ]
    created = store.create_many(
        context.scope,
        context.group_id,
        context.user_id,
        drafts,
        context.now,
        context.reject_past_reminder,
    )[0]
    return ToolResult(
        ok=True,
        status="success",
        message=f"已添加待办：{format_inline(created)}\n提醒时间：{format_time(created.remind_at, context.timezone)}",
        data={"item": todo_to_dict(created, context.timezone)},
    )


def build_tool_specs(
    handlers: dict[str, Callable[[dict[str, Any]], ToolResult]],
) -> dict[str, ToolSpec]:
    """构建新增用途工具定义。

    Args:
        handlers: 以工具名为键的后端执行函数映射。

    Returns:
        新增工具的 ToolSpec 映射。
    """

    return {
        "create_todo": ToolSpec(
            "create_todo",
            "创建一条待办。LLM 只负责提供结构化参数，真正写库由后端完成。",
            object_schema(
                {
                    "title": {"type": "string", "minLength": 1},
                    "content": {"type": ["string", "null"]},
                    "reminder_at": {"type": ["string", "null"]},
                    "due_at": {"type": ["string", "null"]},
                    "reminder_text": {"type": ["string", "null"]},
                    "raw_text": {"type": ["string", "null"]},
                },
                required=["title"],
            ),
            handlers["create_todo"],
        ),
    }
