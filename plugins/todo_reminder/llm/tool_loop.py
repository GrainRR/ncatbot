"""Todo Tool Loop 协调逻辑。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..todo_manage_tools import ToolResult, TodoToolContext, TodoToolExecutor
from ..todo_store import STATUS_DONE, STATUS_OPEN, TodoReminder, TodoStore
from .openai_chat import ChatCompletionChoice, OpenAICompatibleChatClient


TODO_TOOL_LOOP_SYSTEM_PROMPT = (
    "你是 Todo 工具选择器。你不能直接声称数据库操作成功，也不能编造数据库结果。"
    "凡是新增、完成、修改、取消、恢复、删除、合并、推迟、提前、延后、改提醒、改时间等写操作，"
    "必须调用一个最合适的工具。工具参数里的 number 只能使用用户当前可见编号，禁止使用或伪造数据库 id。"
    "提醒展示风格由后端发送提醒时处理，不要把猫娘语气写入标题、内容、时间或工具结果。"
    "如果用户信息不足，请直接用简短中文询问澄清，不要声称已经操作。"
)

# 判断模型普通文本是否在未调用工具时声称操作成功
_FAKE_SUCCESS_KEYWORDS = (
    "已完成",
    "已经完成",
    "完成了",
    "已删除",
    "已经删除",
    "删除了",
    "已新增",
    "已经新增",
    "已添加",
    "已经添加",
    "添加了",
    "已修改",
    "已经修改",
    "修改好了",
    "已取消",
    "已经取消",
    "已恢复",
    "已经恢复",
    "已合并",
    "已经合并",
    "已推迟",
    "已经推迟",
    "已提前",
    "已经提前",
)


@dataclass(frozen=True)
class TodoToolLoopResponse:
    """Todo Tool Loop 的最终回复。"""

    message: str
    tool_results: list[ToolResult]
    llm_content: str = ""


class TodoToolLoop:
    """让 LLM 只选择工具，所有数据库变更由后端工具执行。"""

    def __init__(
        self,
        config: dict[str, Any],
        store: TodoStore,
        client: Any | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client or OpenAICompatibleChatClient(config)

    async def run(self, user_text: str, context: TodoToolContext) -> TodoToolLoopResponse:
        executor = TodoToolExecutor(self.store, context)
        choice = await self.client.complete_with_tools(
            self._build_messages(user_text, context),
            executor.tool_definitions,
        )
        if not isinstance(choice, ChatCompletionChoice):
            choice = ChatCompletionChoice(
                content=str(getattr(choice, "content", "") or ""),
                tool_calls=list(getattr(choice, "tool_calls", []) or []),
            )

        if not choice.tool_calls:
            content = choice.content.strip()
            if contains_fake_success_claim(content):
                return TodoToolLoopResponse(
                    message="操作没有执行：模型没有调用后端工具，数据库未变更。请重新说明要操作的待办编号。",
                    tool_results=[],
                    llm_content=content,
                )
            return TodoToolLoopResponse(
                message=content or "需要补充要操作的待办编号或时间",
                tool_results=[],
                llm_content=content,
            )

        results: list[ToolResult] = []
        for tool_call in choice.tool_calls:
            result = executor.execute(tool_call.name, tool_call.arguments)
            results.append(result)
            if result.status in {"error", "clarify", "confirm"}:
                return TodoToolLoopResponse(
                    message=result.message,
                    tool_results=results,
                    llm_content=choice.content,
                )
        return TodoToolLoopResponse(
            message="\n".join(result.message for result in results),
            tool_results=results,
            llm_content=choice.content,
        )

    def _build_messages(
        self,
        user_text: str,
        context: TodoToolContext,
    ) -> list[dict[str, str]]:
        pending = self.store.list_by_status(
            context.scope,
            context.group_id,
            context.user_id,
            STATUS_OPEN,
            20,
        )
        completed = self.store.list_by_status(
            context.scope,
            context.group_id,
            context.user_id,
            STATUS_DONE,
            20,
        )
        now = datetime.fromtimestamp(context.now, context.timezone)
        user_prompt = (
            "运行时上下文：\n"
            f"current_time: {now:%Y-%m-%d %H:%M:%S}\n"
            f"timezone: {context.timezone.key if hasattr(context.timezone, 'key') else context.timezone}\n"
            f"last_todo_no: {context.last_todo_no}\n\n"
            "用户当前可见未完成待办：\n"
            f"{_format_visible_todos(pending)}\n\n"
            "用户当前可见已完成待办：\n"
            f"{_format_visible_todos(completed)}\n\n"
            "用户原文：\n"
            f"{user_text}"
        )
        return [
            {"role": "system", "content": TODO_TOOL_LOOP_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]


def contains_fake_success_claim(content: str) -> bool:
    """判断模型普通文本是否在未调用工具时声称操作成功。"""

    normalized = content.strip()
    return bool(normalized) and any(keyword in normalized for keyword in _FAKE_SUCCESS_KEYWORDS)


def _format_visible_todos(items: list[TodoReminder]) -> str:
    if not items:
        return "无"
    rows = []
    for item in items:
        reminder_at = _format_timestamp(item.remind_at)
        due_at = _format_timestamp(item.due_at)
        rows.append(
            f"[{item.todo_no}] {item.title} | status={item.status} | reminder_at={reminder_at} | due_at={due_at}"
        )
    return "\n".join(rows)


def _format_timestamp(value: int | None) -> str:
    return "未设置" if value is None else str(value)
