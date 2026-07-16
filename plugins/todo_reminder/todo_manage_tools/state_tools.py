"""状态工具：完成、取消、恢复和永久删除。"""

from __future__ import annotations

from typing import Any

from ..todo_store import STATUS_DELETED, STATUS_DONE, STATUS_OPEN
from .contracts import ToolResult, ToolRuntime, ToolSpec
from .presentation import format_inline, todo_to_dict
from .targets import (
    batch_state_changed_result,
    history_ids_from_args,
    number_from_args,
    numbers_from_args,
    resolve_history_todo,
    resolve_open_todo,
)
from .validation import (
    clean_optional_text,
    history_targets_schema,
    numbers_schema,
    object_schema,
    parse_optional_time,
    target_schema,
)


__all__ = ["tool_specs"]


def tool_specs() -> tuple[ToolSpec, ...]:
    """返回状态变更工具定义。"""

    return (
        ToolSpec(
            "complete_todos",
            "完成一个或多个未完成待办。编号必须是用户当前可见编号。",
            numbers_schema(required=[]),
            _complete_todos,
        ),
        ToolSpec(
            "cancel_todo",
            "取消一条未完成待办，这是软删除路径，不需要永久删除确认。",
            target_schema(required=[]),
            _cancel_todo,
        ),
        ToolSpec(
            "restore_todos",
            "恢复已完成或已取消待办，必须使用稳定 history_id；已发送提醒的记录必须提供新的未来 reminder_at。",
            history_targets_schema(),
            _restore_todos,
        ),
        ToolSpec(
            "delete_todos",
            "永久删除待办必须使用稳定 history_id。首次调用只生成确认令牌；confirmed 会被忽略，后续必须提供 confirmation_token 和相同目标。",
            object_schema(
                {
                    "history_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                    },
                    "history_id": {"type": ["string", "null"], "minLength": 1},
                    "confirmation_token": {"type": ["string", "null"], "minLength": 1},
                    "confirmed": {"type": "boolean"},
                },
                required=[],
            ),
            _delete_todos,
        ),
    )


def _complete_todos(runtime: ToolRuntime, args: dict[str, Any]) -> ToolResult:
    numbers = numbers_from_args(args, runtime)
    items = [resolve_open_todo(runtime, number, "完成") for number in numbers]
    completed = runtime.store.complete_many([item.id for item in items], runtime.context.user_id)
    if completed is None:
        return batch_state_changed_result("完成", "numbers", numbers)
    return ToolResult(
        True,
        "success",
        "已完成待办：" + "、".join(format_inline(item) for item in completed),
        {"items": [todo_to_dict(item, runtime.context.timezone) for item in completed]},
    )


def _cancel_todo(runtime: ToolRuntime, args: dict[str, Any]) -> ToolResult:
    number = number_from_args(args, runtime)
    item = resolve_open_todo(runtime, number, "取消")
    canceled = runtime.store.cancel(item.id, runtime.context.user_id)
    if canceled is None:
        return batch_state_changed_result("取消", "numbers", [number])
    return ToolResult(
        True,
        "success",
        f"已取消待办：{format_inline(canceled)}",
        {"item": todo_to_dict(canceled, runtime.context.timezone)},
    )


def _restore_todos(runtime: ToolRuntime, args: dict[str, Any]) -> ToolResult:
    history_ids = history_ids_from_args(args)
    new_remind_at = parse_optional_time(args.get("reminder_at"), "reminder_at", runtime.context)
    items = [
        resolve_history_todo(runtime, history_id, (STATUS_DONE, STATUS_DELETED), "恢复")
        for history_id in history_ids
    ]
    restored = runtime.store.restore_many(
        [item.id for item in items],
        runtime.context.user_id,
        runtime.context.now,
        runtime.context.reject_past_reminder,
        new_remind_at,
    )
    if restored is None:
        return batch_state_changed_result("恢复", "history_ids", history_ids)
    return ToolResult(
        True,
        "success",
        "已恢复待办：" + "、".join(format_inline(item) for item in restored),
        {"items": [todo_to_dict(item, runtime.context.timezone) for item in restored]},
    )


def _delete_todos(runtime: ToolRuntime, args: dict[str, Any]) -> ToolResult:
    history_ids = history_ids_from_args(args)
    token = clean_optional_text(args.get("confirmation_token"))
    if token is None:
        items = [resolve_history_todo(runtime, history_id, None, "永久删除") for history_id in history_ids]
        confirmation = runtime.store.create_permanent_delete_confirmation(
            runtime.context.user_id,
            items,
            runtime.context.now,
            runtime.context.permanent_delete_confirmation_ttl_seconds,
        )
        return ToolResult(
            False,
            "confirm",
            "永久删除不可恢复。请在令牌有效期内使用此确认令牌再次确认："
            f"{confirmation.token}\n目标："
            + "、".join(format_inline(item) for item in items),
            {
                "items": [todo_to_dict(item, runtime.context.timezone) for item in items],
                "deleted": False,
                "confirmation_token": confirmation.token,
                "expires_at": confirmation.expires_at,
                "history_ids": list(confirmation.target_history_ids),
            },
        )
    deleted = runtime.store.permanently_delete_confirmed(
        token, runtime.context.user_id, history_ids, runtime.context.now
    )
    return ToolResult(
        True,
        "success",
        "已永久删除待办：" + "、".join(format_inline(item) for item in deleted),
        {"items": [todo_to_dict(item, runtime.context.timezone) for item in deleted], "deleted": True},
    )
