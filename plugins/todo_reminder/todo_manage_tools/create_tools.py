"""创建工具：create_todo。"""

from __future__ import annotations

from typing import Any

from ..todo_store import TodoReminderDraft
from .contracts import ToolExecutionStop, ToolResult, ToolRuntime, ToolSpec
from .presentation import format_inline, format_time, todo_to_dict
from .validation import (
    clean_optional_text,
    clean_required_text,
    fallback_reminder_text,
    object_schema,
    parse_optional_time,
)


__all__ = ["tool_specs"]


def tool_specs() -> tuple[ToolSpec, ...]:
    """返回创建工具定义。"""

    return (
        ToolSpec(
            "create_todo",
            "创建一条待办。LLM 只提供结构化参数，真正写库由后端完成。",
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
            _create_todo,
        ),
    )


def _create_todo(runtime: ToolRuntime, args: dict[str, Any]) -> ToolResult:
    """创建一条待办，并在写库前完成模式、容量与时间规则校验。

    Args:
        runtime: 后端注入的可信存储和会话上下文；LLM 无法指定其中的用户、
            群组或当前时间。
        args: 已通过 Schema 校验的创建参数。`title` 必填；`reminder_at`
            和 `due_at` 为用户时区时间文本；`reminder_text` 在猫娘模式下
            必须由调用方显式提供。

    Returns:
        创建成功的待办及用户可读的提醒时间。

    Raises:
        ToolExecutionStop: 标题为空、猫娘提醒文案缺失或未完成待办达到上限时。
        ReminderTimeValidationError: 提醒时间不晚于可信上下文的当前时间时，
            由执行器统一转换为结构化错误。
    """

    context = runtime.context
    title = clean_required_text(args.get("title"), "title")
    content = clean_optional_text(args.get("content"))
    raw_text = clean_optional_text(args.get("raw_text")) or context.user_text or title
    reminder_at = parse_optional_time(args.get("reminder_at"), "reminder_at", context)
    due_at = parse_optional_time(args.get("due_at"), "due_at", context)
    reminder_text = clean_optional_text(args.get("reminder_text"))
    if context.reminder_mode == "catgirl" and not reminder_text:
        raise ToolExecutionStop(
            ToolResult(False, "error", "猫娘模式创建待办需要生成提醒文案，待办没有写入", {})
        )
    if not reminder_text:
        reminder_text = fallback_reminder_text(title)
    if runtime.store.count_pending(context.scope, context.group_id, context.user_id) >= context.max_pending:
        raise ToolExecutionStop(
            ToolResult(
                False,
                "error",
                f"未完成待办已经达到上限 {context.max_pending} 条，请先完成或取消一些待办",
                {},
            )
        )
    created = runtime.store.create_many(
        context.scope,
        context.group_id,
        context.user_id,
        [
            TodoReminderDraft(
                title=title,
                content=content,
                raw_text=raw_text,
                remind_at=reminder_at,
                due_at=due_at,
                reminder_text=reminder_text,
                llm_json={"tool": "create_todo", "arguments": args},
            )
        ],
        context.now,
        context.reject_past_reminder,
    )[0]
    return ToolResult(
        True,
        "success",
        f"已添加待办：{format_inline(created)}\n提醒时间：{format_time(created.remind_at, context.timezone)}",
        {"item": todo_to_dict(created, context.timezone)},
    )
