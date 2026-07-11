"""Todo Tool Loop 的旧命令兼容和文本预处理层。

这个模块保留 `llm_parser` 文件名，是为了兼容旧版 `#待办 ...` 话术。
它不再调用 LLM，也不产出可直接写库的待办草稿；所有数据库变更仍必须
经过 Todo Tool Loop 的工具白名单、schema 校验、编号解析和状态校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


HASH_TODO_PREFIX = "#待办"
TODO_PREPROCESS_NOT_TODO = "not_todo"
TODO_PREPROCESS_PENDING = "pending"
TODO_PREPROCESS_COMPLETED = "completed"
TODO_PREPROCESS_TOOL_LOOP = "tool_loop"
TODO_PREPROCESS_CLARIFY = "clarify"

_PENDING_QUERY_WORDS = {
    "",
    "查看",
    "列表",
    "查看列表",
    "查看待办",
    "待办列表",
    "未完成",
    "查看未完成",
    "查看未完成待办",
}
_COMPLETED_QUERY_WORDS = {
    "查看已完成",
    "已完成",
    "已完成待办",
    "查看完成",
    "完成列表",
}
_AMBIGUOUS_WORDS = {
    "?",
    "？",
    "帮助",
    "help",
    "怎么用",
    "说明",
}
_WRITE_KEYWORDS = (
    "新增",
    "添加",
    "创建",
    "新建",
    "加个",
    "加一条",
    "完成",
    "修改",
    "更改",
    "取消",
    "恢复",
    "删除",
    "永久删除",
    "合并",
    "推迟",
    "延后",
    "提前",
    "晚点",
    "稍后",
    "改提醒",
    "改时间",
    "提醒调整",
)
_OLD_CREATE_PREFIXES = (
    "新增",
    "添加",
    "创建",
    "新建",
    "加个",
    "加一条",
)


@dataclass(frozen=True)
class TodoCommandPreprocessResult:
    """`#待办` 兼容预处理结果。"""

    route: str
    original_text: str
    command_text: str
    normalized_text: str
    clarify_message: str = ""

    @property
    def is_hash_todo(self) -> bool:
        return self.route != TODO_PREPROCESS_NOT_TODO


class TodoParseError(Exception):
    """兼容旧调用方的解析错误类型。"""


class TodoLlmParser:
    """旧 `TodoLlmParser` 名称的兼容适配器。

    这个类不再承担独立 LLM 解析职责。旧代码如果仍实例化它，只能得到
    预处理结果；不能通过它绕过 Tool Loop 写数据库。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}

    def preprocess(self, text: str) -> TodoCommandPreprocessResult:
        return preprocess_todo_command(text)

    async def parse(self, user_text: str, reminder_mode: str = "catgirl") -> list[Any]:
        raise TodoParseError("llm_parser 已改为兼容预处理层，请通过 Todo Tool Loop 执行待办操作")


def preprocess_todo_command(text: str) -> TodoCommandPreprocessResult:
    """识别并规范化 `#待办` 命令文本。

    Args:
        text: 用户原始消息。

    Returns:
        预处理结果。`tool_loop` 的 `normalized_text` 可直接作为 Tool Loop 的用户文本。
    """

    original = text or ""
    normalized = " ".join(original.strip().split())
    if normalized != HASH_TODO_PREFIX and not normalized.startswith(f"{HASH_TODO_PREFIX} "):
        return TodoCommandPreprocessResult(
            route=TODO_PREPROCESS_NOT_TODO,
            original_text=original,
            command_text=normalized,
            normalized_text=normalized,
        )

    command_text = normalized[len(HASH_TODO_PREFIX) :].strip()
    return preprocess_hash_todo_content(command_text, original_text=original)


def preprocess_hash_todo_content(
    content: str,
    original_text: str | None = None,
) -> TodoCommandPreprocessResult:
    """规范化 `#待办` 后面的内容。"""

    command_text = " ".join((content or "").strip().split())
    original = original_text if original_text is not None else f"{HASH_TODO_PREFIX} {command_text}".strip()

    if command_text in _PENDING_QUERY_WORDS:
        return TodoCommandPreprocessResult(
            route=TODO_PREPROCESS_PENDING,
            original_text=original,
            command_text=command_text,
            normalized_text="查看待办",
        )
    if command_text in _COMPLETED_QUERY_WORDS:
        return TodoCommandPreprocessResult(
            route=TODO_PREPROCESS_COMPLETED,
            original_text=original,
            command_text=command_text,
            normalized_text="查看已完成",
        )
    if command_text in _AMBIGUOUS_WORDS:
        return TodoCommandPreprocessResult(
            route=TODO_PREPROCESS_CLARIFY,
            original_text=original,
            command_text=command_text,
            normalized_text=command_text,
            clarify_message="请说明要查看、添加、修改、完成还是调整哪条待办",
        )
    if not command_text:
        return TodoCommandPreprocessResult(
            route=TODO_PREPROCESS_PENDING,
            original_text=original,
            command_text=command_text,
            normalized_text="查看待办",
        )

    normalized_text = command_text
    if not _looks_like_write_operation(command_text):
        # 兼容旧版 `#待办 明天十点提醒我开会` 和 `#待办 买牛奶` 创建习惯。
        normalized_text = f"新增待办：{command_text}"
    return TodoCommandPreprocessResult(
        route=TODO_PREPROCESS_TOOL_LOOP,
        original_text=original,
        command_text=command_text,
        normalized_text=normalized_text,
    )


def render_reminder_text(title: str, note: str | None, mode: str = "catgirl") -> str:
    """按展示风格生成到点提醒文案。

    风格只影响发送出去的展示文案，不应写回待办标题、备注或时间字段。
    """

    clean_title = _clean_text(title) or "待办"
    clean_note = _clean_text(note)
    if mode == "catgirl":
        text = f"主人，该做「{clean_title}」啦喵，已经到提醒时间了~"
        if clean_note:
            text += f"\n备注：{clean_note}"
        return text
    text = f"待办提醒：{clean_title}"
    if clean_note:
        text += f"\n备注：{clean_note}"
    return text


def parse_local_datetime(value: str, timezone: ZoneInfo) -> datetime:
    """解析已经规范化的本地时间字符串。

    这里只处理绝对时间格式；自然语言时间仍交给 Tool Loop 里的 LLM 选择工具，
    后端工具负责最终校验和持久化。
    """

    normalized = value.strip().replace("T", " ")
    if normalized.endswith("Z") or "+" in normalized[10:] or "-" in normalized[10:]:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00")).astimezone(timezone)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone)
        except ValueError:
            continue
    raise TodoParseError("时间格式不正确，需要 YYYY-MM-DD HH:MM:SS")


def _looks_like_write_operation(text: str) -> bool:
    if any(text.startswith(prefix) for prefix in _OLD_CREATE_PREFIXES):
        return True
    return any(keyword in text for keyword in _WRITE_KEYWORDS)


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
