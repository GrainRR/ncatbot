"""Todo Tool Loop 协调逻辑。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..todo_manage_tools import ToolResult, TodoToolContext, TodoToolExecutor
from ..todo_store import STATUS_DELETED, STATUS_DONE, STATUS_OPEN, TodoReminder, TodoStore
from .openai_chat import ChatCompletionChoice, OpenAICompatibleChatClient


TODO_TOOL_LOOP_SYSTEM_PROMPT = (
    "你是 Todo 工具选择器。你不能直接声称数据库操作成功，也不能编造数据库结果。"
    "凡是新增、完成、修改、取消、恢复、删除、合并、推迟、提前、延后、改提醒、改时间等写操作，"
    "必须调用一个最合适的工具。number 只能用于当前未完成待办；恢复和永久删除必须使用列表中的 history_id，禁止使用或伪造数据库 id。"
    "永久删除首次调用只会返回确认令牌；confirmed=true 不能删除，第二次调用必须带相同 history_id 和 confirmation_token。"
    "创建待办时，如果运行时 reminder_mode 是 catgirl，必须在 create_todo.reminder_text 中写入猫娘风格提醒文案；"
    "如果 reminder_mode 是 concise，reminder_text 使用简洁直接的提醒文案或 null。"
    "不要把猫娘语气写入 title、content、reminder_at、due_at 或工具结果。"
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
    """Todo Tool Loop 的最终回复。

    该对象同时保留用户可见消息、真实工具执行结果和模型普通文本，便于
    路由层记录上下文编号并在测试中断言模型是否越权声称成功。
    """

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
        """创建 Todo Tool Loop 协调器。

        Args:
            config: 插件配置，包含 LLM 连接参数。
            store: Todo 存储层实例。
            client: 可选 LLM 客户端；测试可注入假客户端。
        """

        self.config = config
        self.store = store
        self.client = client or OpenAICompatibleChatClient(config)

    async def run(self, user_text: str, context: TodoToolContext) -> TodoToolLoopResponse:
        """执行一次 Todo Tool Loop。

        流程是把用户原文、当前可见待办和工具定义交给 LLM，让模型只选择
        工具；如果模型返回普通文本且声称操作成功，则拦截并明确说明数据库
        未变更。所有真正的数据库操作都由 TodoToolExecutor 完成。

        Args:
            user_text: 用户输入的 Todo 操作文案。
            context: 程序路由层生成的可信工具上下文。

        Returns:
            基于真实工具结果生成的最终回复。
        """

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
        """构建发送给 LLM 的消息。

        Args:
            user_text: 用户输入的 Todo 操作文案。
            context: 程序路由层生成的可信工具上下文。

        Returns:
            包含系统约束、运行时上下文、当前可见待办和用户原文的消息列表。
        """

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
        canceled = self.store.list_by_status(
            context.scope,
            context.group_id,
            context.user_id,
            STATUS_DELETED,
            20,
        )
        now = datetime.fromtimestamp(context.now, context.timezone)
        user_prompt = (
            "运行时上下文：\n"
            f"current_time: {now:%Y-%m-%d %H:%M:%S}\n"
            f"timezone: {context.timezone.key if hasattr(context.timezone, 'key') else context.timezone}\n"
            f"reminder_mode: {context.reminder_mode}\n"
            f"last_todo_no: {context.last_todo_no}\n\n"
            "用户当前可见未完成待办：\n"
            f"{_format_visible_todos(pending)}\n\n"
            "用户当前可见已完成待办：\n"
            f"{_format_visible_todos(completed)}\n\n"
            "用户当前可见已取消待办：\n"
            f"{_format_visible_todos(canceled)}\n\n"
            "用户原文：\n"
            f"{user_text}"
        )
        return [
            {"role": "system", "content": TODO_TOOL_LOOP_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]


def contains_fake_success_claim(content: str) -> bool:
    """判断模型普通文本是否在未调用工具时声称操作成功。

    Args:
        content: 模型未调用工具时返回的普通文本。

    Returns:
        文本包含“已完成/已删除/已新增”等成功语义时返回 True。
    """

    normalized = content.strip()
    return bool(normalized) and any(keyword in normalized for keyword in _FAKE_SUCCESS_KEYWORDS)


def _format_visible_todos(items: list[TodoReminder]) -> str:
    """格式化当前可见待办，作为 LLM 选择工具的上下文。

    Args:
        items: 当前用户当前范围内可见的待办列表。

    Returns:
        面向模型的紧凑文本；没有待办时返回 `无`。
    """

    if not items:
        return "无"
    rows = []
    for item in items:
        reminder_at = _format_timestamp(item.remind_at)
        due_at = _format_timestamp(item.due_at)
        rows.append(
            f"[{item.todo_no}] {item.title} | history_id={item.history_id} | status={item.status} | reminder_at={reminder_at} | due_at={due_at}"
        )
    return "\n".join(rows)


def _format_timestamp(value: int | None) -> str:
    """格式化传给模型的时间戳。

    Args:
        value: Unix 秒级时间戳或 None。

    Returns:
        原始时间戳文本；未设置时返回 `未设置`。
    """

    return "未设置" if value is None else str(value)
