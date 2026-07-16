"""Todo 状态变更用途工具。"""

from __future__ import annotations

from typing import Any, Callable

from ..todo_store import (
    ReminderReconfigurationRequiredError,
    ReminderTimeValidationError,
    STATUS_DELETED,
    STATUS_DONE,
    STATUS_OPEN,
    TodoConfirmationError,
    TodoReminder,
    TodoStore,
)
from .common import (
    TodoToolContext,
    ToolResult,
    ToolSpec,
    ToolExecutionStop,
    clean_optional_text,
    format_inline,
    number_from_args,
    numbers_from_args,
    numbers_schema,
    object_schema,
    parse_optional_time,
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
    completed = store.complete_many([item.id for item in items], context.user_id)
    if completed is None:
        return _atomic_failure("完成", "numbers", numbers)
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
    canceled = store.cancel(item.id, context.user_id)
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

    history_ids = _history_ids_from_args(args)
    new_remind_at = parse_optional_time(args.get("reminder_at"), "reminder_at", context)
    items = [_resolve_history_todo(store, context, value, (STATUS_DONE, STATUS_DELETED), "恢复") for value in history_ids]
    try:
        restored = store.restore_many(
            [item.id for item in items],
            context.user_id,
            context.now,
            context.reject_past_reminder,
            new_remind_at,
        )
    except ReminderReconfigurationRequiredError as exc:
        return _restore_reminder_required_error(exc)
    except ReminderTimeValidationError as exc:
        return _reminder_error(exc)
    if restored is None:
        return _atomic_failure("恢复", "history_ids", history_ids)
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

    history_ids = _history_ids_from_args(args)
    token = clean_optional_text(args.get("confirmation_token"))
    if token is None:
        items = [_resolve_history_todo(store, context, value, None, "永久删除") for value in history_ids]
        confirmation = store.create_permanent_delete_confirmation(
            context.user_id,
            items,
            context.now,
            context.permanent_delete_confirmation_ttl_seconds,
        )
        return ToolResult(
            ok=False,
            status="confirm",
            message=(
                "永久删除不可恢复。请在令牌有效期内使用此确认令牌再次确认："
                f"{confirmation.token}\n目标："
                + "、".join(format_inline(item) for item in items)
            ),
            data={
                "items": [todo_to_dict(item, context.timezone) for item in items],
                "deleted": False,
                "confirmation_token": confirmation.token,
                "expires_at": confirmation.expires_at,
                "history_ids": list(confirmation.target_history_ids),
            },
        )
    try:
        deleted = store.permanently_delete_confirmed(token, context.user_id, history_ids, context.now)
    except TodoConfirmationError as exc:
        return _confirmation_error(exc)
    return ToolResult(
        ok=True,
        status="success",
        message="已永久删除待办：" + "、".join(format_inline(item) for item in deleted),
        data={"items": [todo_to_dict(item, context.timezone) for item in deleted], "deleted": True},
    )


def _history_ids_from_args(args: dict[str, Any]) -> list[str]:
    """从参数中读取一个或多个稳定历史 ID。"""

    values = args.get("history_ids")
    if values:
        history_ids = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        if history_ids:
            return history_ids
    history_id = clean_optional_text(args.get("history_id"))
    if history_id:
        return [history_id]
    raise ToolExecutionStop(
        ToolResult(False, "clarify", "请提供待办列表中显示的历史 ID（例如 H-...）", {})
    )


def _resolve_history_todo(
    store: TodoStore,
    context: TodoToolContext,
    history_id: str,
    statuses: tuple[str, ...] | None,
    action: str,
) -> TodoReminder:
    """按稳定历史 ID 查找目标，禁止按可复用序号猜测历史记录。"""

    item = store.find_by_history_id(context.user_id, history_id, statuses)
    if item is not None:
        return item
    existing = store.find_by_history_id(context.user_id, history_id, None)
    if existing is not None:
        raise ToolExecutionStop(
            ToolResult(
                False,
                "error",
                f"历史 ID {history_id} 的待办当前状态不允许{action}",
                {"history_id": history_id, "status": existing.status},
            )
        )
    raise ToolExecutionStop(
        ToolResult(False, "error", f"找不到历史 ID {history_id} 对应的待办", {"history_id": history_id})
    )


def _atomic_failure(action: str, target_field: str, targets: list[Any]) -> ToolResult:
    """表示事务内复核失败且没有部分写入。"""

    return ToolResult(
        False,
        "error",
        f"待办状态或归属已变化，批量{action}未执行，数据没有部分修改",
        {target_field: targets, "atomic": True},
    )


def _reminder_error(exc: ReminderTimeValidationError) -> ToolResult:
    """把统一提醒时间校验异常转换为结构化工具错误。"""

    return ToolResult(
        False,
        "error",
        "提醒时间必须晚于当前时间，待办没有写入",
        {"code": "remind_at_not_future", "remind_at": exc.remind_at, "now": exc.now},
    )


def _restore_reminder_required_error(exc: ReminderReconfigurationRequiredError) -> ToolResult:
    """提示已发送提醒的记录必须提供新的未来提醒时间。"""

    return ToolResult(
        False,
        "clarify",
        "该待办已发送过提醒；恢复前请重新设置未来提醒时间",
        {"code": "future_remind_at_required", "history_ids": list(exc.history_ids)},
    )


def _confirmation_error(exc: TodoConfirmationError) -> ToolResult:
    """把确认令牌失败转换为不写库的结构化错误。"""

    messages = {
        "confirmation_invalid": "永久删除确认令牌无效，待办没有删除",
        "confirmation_expired": "永久删除确认令牌已过期，待办没有删除",
        "confirmation_target_mismatch": "确认目标与首次请求不一致，待办没有删除",
        "confirmation_target_changed": "确认期间待办状态已变化，待办没有删除",
    }
    return ToolResult(False, "error", messages.get(exc.code, "永久删除确认失败，待办没有删除"), {"code": exc.code, **exc.data})


def _history_targets_schema() -> dict[str, Any]:
    """构建仅允许稳定历史 ID 的目标参数 schema。"""

    return object_schema(
        {
            "history_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
            "history_id": {"type": ["string", "null"], "minLength": 1},
            "reminder_at": {"type": ["string", "null"]},
        },
        required=[],
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
            "恢复一个或多个已完成或已取消待办，必须使用稳定 history_id；已发送提醒的记录必须提供新的未来 reminder_at。",
            _history_targets_schema(),
            handlers["restore_todos"],
        ),
        "delete_todos": ToolSpec(
            "delete_todos",
            "永久删除一个或多个待办，必须使用稳定 history_id。首次调用只生成确认令牌，confirmed 会被忽略。",
            object_schema(
                {
                    "history_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
                    "history_id": {"type": ["string", "null"], "minLength": 1},
                    "confirmation_token": {"type": ["string", "null"], "minLength": 1},
                    "confirmed": {"type": "boolean"},
                },
                required=[],
            ),
            handlers["delete_todos"],
        ),
    }
