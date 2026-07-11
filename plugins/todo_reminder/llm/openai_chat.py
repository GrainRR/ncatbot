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
    """模型返回的单个工具调用。"""

    name: str
    arguments: dict[str, Any]
    call_id: str | None = None


@dataclass(frozen=True)
class ChatCompletionChoice:
    """模型返回的首选消息。"""

    content: str
    tool_calls: list[ChatToolCall]


class LlmRequestError(Exception):
    """LLM 请求失败。"""


class OpenAICompatibleChatClient:
    """调用 OpenAI 兼容的 chat/completions 接口。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
    ) -> ChatCompletionChoice:
        return await asyncio.to_thread(self._complete_with_tools, messages, tools)

    def _complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
    ) -> ChatCompletionChoice:
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
        explicit = str(self.config.get("llm_api_url") or "").strip()
        if explicit:
            return explicit
        base = str(self.config.get("llm_api_base") or "").strip().rstrip("/")
        if not base:
            return ""
        return f"{base}/chat/completions"

    def _api_key(self) -> str:
        key = str(self.config.get("llm_api_key") or "").strip()
        if key:
            return key
        env_name = str(self.config.get("llm_api_key_env") or "").strip()
        return os.environ.get(env_name, "").strip() if env_name else ""


def _extract_choice(data: dict[str, Any]) -> ChatCompletionChoice:
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
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        return "".join(parts)
    return ""


def _extract_tool_calls(message: dict[str, Any]) -> list[ChatToolCall]:
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
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
