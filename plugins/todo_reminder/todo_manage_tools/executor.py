"""唯一工具执行边界：白名单、Schema、调度与异常转换。"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..todo_store import (
    ReminderReconfigurationRequiredError,
    ReminderTimeValidationError,
    TodoConfirmationError,
    TodoStore,
)
from .contracts import (
    TodoToolContext,
    ToolRuntime,
    ToolExecutionStop,
    ToolResult,
    ToolValidationError,
)
from .registry import TOOL_SPECS_BY_NAME, openai_tool_definitions
from .validation import reminder_validation_result, validate_json_schema


class TodoToolExecutor:
    """调用注册表中的处理器，不承载任何具体待办业务。"""

    def __init__(self, store: TodoStore, context: TodoToolContext) -> None:
        self.runtime = ToolRuntime(store=store, context=context)

    @property
    def tool_definitions(self) -> list[dict[str, object]]:
        """返回注册表派生的 OpenAI 工具定义。"""

        return openai_tool_definitions()

    def execute(self, tool_name: str, args: dict[str, Any]) -> ToolResult:
        """校验并调度单个白名单工具。"""

        spec = TOOL_SPECS_BY_NAME.get(tool_name)
        if spec is None:
            return ToolResult(False, "error", f"不支持的待办工具：{tool_name}", {"tool": tool_name})
        try:
            validate_json_schema(args, spec.parameters)
            return spec.handler(self.runtime, args)
        except ToolExecutionStop as exc:
            return exc.result
        except ToolValidationError as exc:
            return ToolResult(False, "error", f"工具参数不合法：{exc}", {"tool": tool_name})
        except ReminderReconfigurationRequiredError as exc:
            return ToolResult(
                False,
                "clarify",
                "该待办已发送过提醒；恢复前请重新设置未来提醒时间",
                {"code": "future_remind_at_required", "history_ids": list(exc.history_ids)},
            )
        except ReminderTimeValidationError as exc:
            return reminder_validation_result(exc)
        except TodoConfirmationError as exc:
            return _confirmation_error(exc)
        except sqlite3.Error:
            return ToolResult(
                False,
                "error",
                "待办数据存储失败，操作已回滚",
                {"tool": tool_name, "code": "storage_error"},
            )
        except ValueError as exc:
            return ToolResult(False, "error", f"待办操作参数无效：{exc}", {"tool": tool_name})


def _confirmation_error(exc: TodoConfirmationError) -> ToolResult:
    messages = {
        "confirmation_invalid": "永久删除确认令牌无效，待办没有删除",
        "confirmation_expired": "永久删除确认令牌已过期，待办没有删除",
        "confirmation_target_mismatch": "确认目标与首次请求不一致，待办没有删除",
        "confirmation_target_changed": "确认期间待办状态已变化，待办没有删除",
    }
    return ToolResult(
        False,
        "error",
        messages.get(exc.code, "永久删除确认失败，待办没有删除"),
        {"code": exc.code, **exc.data},
    )
