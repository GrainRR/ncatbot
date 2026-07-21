"""合并工具：merge_todos。"""

from __future__ import annotations

from typing import Any

from .contracts import ToolExecutionStop, ToolResult, ToolRuntime, ToolSpec
from .presentation import format_inline, todo_to_dict
from .targets import batch_state_changed_result, numbers_from_args, resolve_open_todo
from .validation import clean_optional_text, object_schema


__all__ = ["tool_specs"]


def tool_specs() -> tuple[ToolSpec, ...]:
    """返回合并工具定义。"""

    return (
        ToolSpec(
            "merge_todos",
            "合并多个未完成待办，保留第一个编号并取消其余编号。",
            object_schema(
                {
                    "numbers": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                        "minItems": 2,
                    },
                    "title": {"type": ["string", "null"]},
                },
                required=["numbers"],
            ),
            _merge_todos,
        ),
    )


def _merge_todos(runtime: ToolRuntime, args: dict[str, Any]) -> ToolResult:
    """原子合并多个未完成待办，保留第一个目标作为合并记录。

    Args:
        runtime: 可信运行时；用户归属和提醒时间规则只从这里取得。
        args: 包含至少两个当前未完成编号的 `numbers`，以及可选合并标题。
            未给标题时按输入顺序拼接原标题。

    Returns:
        合并后的第一条待办和被合并的编号；任一目标在事务内失效时不写入部分结果。

    Raises:
        ToolExecutionStop: 去重后目标不足两条时。
        ReminderTimeValidationError: 合并所得的最早提醒时间不在未来时。
    """

    numbers = numbers_from_args(args, runtime)
    if len(numbers) < 2:
        raise ToolExecutionStop(ToolResult(False, "clarify", "合并待办至少需要两个编号", {"numbers": numbers}))
    items = [resolve_open_todo(runtime, number, "合并") for number in numbers]
    title = clean_optional_text(args.get("title")) or "；".join(item.title for item in items)
    updates = {
        "title": title,
        "content": "\n".join(item.content or item.title for item in items),
        "raw_text": runtime.context.user_text or "合并待办",
        "reminder_text": "",
        "remind_at": min(
            (item.remind_at for item in items if item.remind_at is not None),
            default=None,
        ),
        "due_at": max(
            (item.due_at for item in items if item.due_at is not None),
            default=None,
        ),
    }
    merged = runtime.store.merge_open_todos(
        [item.id for item in items],
        runtime.context.user_id,
        updates,
        runtime.context.now,
        runtime.context.reject_past_reminder,
    )
    if merged is None:
        return batch_state_changed_result("合并", "numbers", numbers)
    return ToolResult(
        True,
        "success",
        f"已合并为待办：{format_inline(merged)}",
        {
            "item": todo_to_dict(merged, runtime.context.timezone),
            "merged_numbers": numbers,
        },
    )
