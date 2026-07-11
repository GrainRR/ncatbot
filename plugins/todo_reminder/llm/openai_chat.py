"""OpenAI 兼容 chat/completions 客户端。"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChatToolCall:
    """模型返回的单个工具调用。

    这里只保存工具名、已解析为字典的参数和可选调用 ID，后续执行仍会由
    后端工具层做白名单与 schema 校验。
    """

    name: str
    arguments: dict[str, Any]
    call_id: str | None = None


@dataclass(frozen=True)
class ChatCompletionChoice:
    """模型返回的首选消息。

    包含普通文本和工具调用列表。Tool Loop 会根据工具调用是否存在决定
    是否允许进入后端执行。
    """

    content: str
    tool_calls: list[ChatToolCall]


class LlmRequestError(Exception):
    """LLM 请求失败。"""


class OpenAICompatibleChatClient:
    """调用 OpenAI 兼容的 chat/completions 接口。"""

    def __init__(self, config: dict[str, Any]) -> None:
        """创建 LLM 客户端。

        Args:
            config: 插件配置，包含接口地址、密钥、模型名和超时时间。
        """

        self.config = config

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
    ) -> ChatCompletionChoice:
        """异步调用带工具定义的 chat/completions。

        Args:
            messages: 发送给模型的消息列表。
            tools: OpenAI compatible tools 定义。

        Returns:
            模型首选回复，包含普通文本和工具调用列表。

        Raises:
            LlmRequestError: 请求失败、超时或响应格式非法。
        """

        return await asyncio.to_thread(self._complete_with_tools, messages, tools)

    def _complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
    ) -> ChatCompletionChoice:
        """在线程中执行同步 HTTP 请求。

        Args:
            messages: 发送给模型的消息列表。
            tools: OpenAI compatible tools 定义。

        Returns:
            解析后的模型首选回复。

        Raises:
            LlmRequestError: LLM 未配置、HTTP 请求失败、超时或响应不是有效 JSON。
        """

        api_url = self._api_url()
        api_key = self._api_key()
        model = str(self.config.get("llm_model") or "").strip()
        if not api_url or not api_key or not model:
            raise LlmRequestError(
                "todo_reminder 还没有配置 LLM。请在 plugin_configs.todo_reminder "
                "里设置 llm_api_base、llm_api_key 和 llm_model"
            )

        payload = {
            "model": model,
            "temperature": 0,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            api_url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = _positive_int(self.config.get("llm_timeout_seconds"), 30)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise LlmRequestError(f"LLM 请求失败：HTTP {exc.code} {error_body[:120]}") from exc
        except urllib.error.URLError as exc:
            raise LlmRequestError(f"LLM 请求失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise LlmRequestError("LLM 请求超时，数据库未变更") from exc

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise LlmRequestError("LLM 接口返回的响应不是有效 JSON") from exc
        return _extract_choice(data)

    def _api_url(self) -> str:
        """解析 LLM 接口地址。

        Returns:
            显式配置的 `llm_api_url`，或由 `llm_api_base` 拼出的
            `/chat/completions` 地址；未配置时返回空字符串。
        """

        explicit = str(self.config.get("llm_api_url") or "").strip()
        if explicit:
            return explicit
        base = str(self.config.get("llm_api_base") or "").strip().rstrip("/")
        if not base:
            return ""
        return f"{base}/chat/completions"

    def _api_key(self) -> str:
        """解析 LLM API key。

        Returns:
            优先返回配置中的 `llm_api_key`；否则按 `llm_api_key_env`
            指定的环境变量读取。
        """

        key = str(self.config.get("llm_api_key") or "").strip()
        if key:
            return key
        env_name = str(self.config.get("llm_api_key_env") or "").strip()
        return os.environ.get(env_name, "").strip() if env_name else ""


def _extract_choice(data: dict[str, Any]) -> ChatCompletionChoice:
    """从 OpenAI compatible 响应中抽取首选消息。

    Args:
        data: 已解析的 JSON 响应。

    Returns:
        标准化后的首选消息。

    Raises:
        LlmRequestError: 响应缺少 choices 或 message 格式不合法。
    """

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmRequestError("LLM 响应里没有 choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LlmRequestError("LLM choices 格式不正确")
    message = first.get("message")
    if not isinstance(message, dict):
        text = first.get("text")
        return ChatCompletionChoice(content=text if isinstance(text, str) else "", tool_calls=[])

    content = _extract_content(message.get("content"))
    tool_calls = _extract_tool_calls(message)
    return ChatCompletionChoice(content=content, tool_calls=tool_calls)


def _extract_content(content: Any) -> str:
    """抽取模型普通文本。

    Args:
        content: message.content 字段，兼容字符串和分段数组格式。

    Returns:
        拼接后的普通文本；没有文本时返回空字符串。
    """

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        return "".join(parts)
    return ""


def _extract_tool_calls(message: dict[str, Any]) -> list[ChatToolCall]:
    """抽取模型工具调用。

    同时兼容新式 `tool_calls` 和旧式 `function_call` 响应格式。

    Args:
        message: 响应中的 message 对象。

    Returns:
        标准化后的工具调用列表。

    Raises:
        LlmRequestError: 工具参数不是 JSON 对象或不是有效 JSON。
    """

    raw_tool_calls = message.get("tool_calls")
    if isinstance(raw_tool_calls, list) and raw_tool_calls:
        calls: list[ChatToolCall] = []
        for raw_call in raw_tool_calls:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            calls.append(
                ChatToolCall(
                    name=name,
                    arguments=_parse_arguments(function.get("arguments")),
                    call_id=str(raw_call.get("id")) if raw_call.get("id") is not None else None,
                )
            )
        return calls

    function_call = message.get("function_call")
    if isinstance(function_call, dict):
        name = function_call.get("name")
        if isinstance(name, str) and name:
            return [
                ChatToolCall(
                    name=name,
                    arguments=_parse_arguments(function_call.get("arguments")),
                    call_id=None,
                )
            ]
    return []


def _parse_arguments(value: Any) -> dict[str, Any]:
    """解析工具调用参数。

    Args:
        value: 模型返回的 function.arguments，可以是字典或 JSON 字符串。

    Returns:
        JSON 对象形式的工具参数。

    Raises:
        LlmRequestError: 参数不是 JSON 对象或 JSON 解析失败。
    """

    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise LlmRequestError("LLM 工具参数不是 JSON 对象")
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise LlmRequestError("LLM 工具参数不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise LlmRequestError("LLM 工具参数不是 JSON 对象")
    return parsed


def _positive_int(value: Any, default: int) -> int:
    """把配置值转换为正整数。

    Args:
        value: 待转换的配置值。
        default: 转换失败或不是正整数时使用的默认值。

    Returns:
        正整数配置值。
    """

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
