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
    """在一个存储事务中完成多个未完成待办。

    Args:
        runtime: 可信运行时；内部用户 ID 是批量事务的归属校验条件。
        args: 已校验的 `numbers`、单个 `number` 或上下文 `reference`。

    Returns:
        全部目标完成后的列表；目标在事务内状态变化时返回 `atomic=True`，
        表示没有任何部分完成。

    Raises:
        ToolExecutionStop: 某个编号在预校验阶段不存在或不是未完成状态时。
    """

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
    """软删除一条未完成待办，并标记其删除原因为用户取消。

    Args:
        runtime: 含当前用户归属的可信运行时。
        args: 用当前未完成编号或上下文引用定位单个目标的参数。

    Returns:
        已取消待办的结构化表示；并发状态变化时不声称取消成功。

    Raises:
        ToolExecutionStop: 目标编号缺失、找不到或目标不是未完成状态时。
    """

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
    """按稳定历史 ID 原子恢复历史待办。

    Args:
        runtime: 可信运行时；`now` 和 `reject_past_reminder` 参与事务内复核。
        args: 一个或多个 `history_id`，以及可选的未来 `reminder_at`。若任何
            目标曾经发送提醒并自动删除，必须提供该新时间，且所有目标共用它。

    Returns:
        全部恢复后的待办列表；发生并发状态变化时返回不含部分写入的原子失败结果。

    Raises:
        ToolExecutionStop: 历史 ID 缺失、不属于当前用户或状态不可恢复时。
        ReminderReconfigurationRequiredError: 已发送提醒的目标没有新的未来提醒时间时，
            由执行器转换为澄清结果。
        ReminderTimeValidationError: 恢复后的提醒时间不在未来时。
    """

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
    """执行永久删除的两阶段确认状态机。

    Args:
        runtime: 可信运行时；用户 ID、当前时间和令牌 TTL 均不能由 LLM 覆盖。
        args: 稳定 `history_id` 目标、可选 `confirmation_token` 和兼容字段
            `confirmed`。`confirmed` 始终被忽略，不能单独授权删除。

    Returns:
        未携带令牌时返回 `confirm` 结果以及令牌、过期时间和冻结的目标；
        令牌存在时仅在用户、目标顺序、版本和过期时间均匹配后删除。

    Raises:
        ToolExecutionStop: 历史 ID 缺失、找不到或不属于当前用户时。
        TodoConfirmationError: 令牌无效、过期或目标已变更时，由执行器转为错误结果。
    """

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
