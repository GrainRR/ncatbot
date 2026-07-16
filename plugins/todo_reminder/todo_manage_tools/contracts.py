"""Todo 工具层的唯一公共契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from ..todo_store import TodoStore


@dataclass(frozen=True)
class TodoToolContext:
    """由路由层构造、LLM 无法伪造的执行上下文。"""

    scope: str
    group_id: str | None
    user_id: str
    now: int
    timezone: ZoneInfo
    max_pending: int = 100
    reject_past_reminder: bool = True
    permanent_delete_confirmation_ttl_seconds: int = 300
    last_todo_no: int | None = None
    reminder_mode: str = "concise"
    user_text: str = ""


@dataclass(frozen=True)
class ToolRuntime:
    """处理器唯一可用的可信运行时依赖。"""

    store: TodoStore
    context: TodoToolContext


@dataclass(frozen=True)
class ToolResult:
    """面向 Tool Loop 的统一结构化执行结果。"""

    ok: bool
    status: str
    message: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化的结构。"""

        return {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }


ToolHandler = Callable[[ToolRuntime, dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    """一个受白名单保护的工具定义及其处理器。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def to_openai_tool(self) -> dict[str, Any]:
        """转换为 OpenAI-compatible tools 结构。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolValidationError(Exception):
    """LLM 工具参数未满足声明的 JSON Schema 子集。"""


class ToolExecutionStop(Exception):
    """处理器要求执行器直接返回指定结果。"""

    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.message)
        self.result = result
