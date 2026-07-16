"""待办工具结果的序列化和用户消息格式化。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..todo_store import STATUS_DELETED, STATUS_DONE, STATUS_OPEN, TodoReminder


def todo_to_dict(item: TodoReminder, timezone: ZoneInfo) -> dict[str, object]:
    """将待办转换为不含数据库内部 ID 的结构化数据。"""

    return {
        "number": item.todo_no,
        "history_id": item.history_id,
        "title": item.title,
        "content": item.content,
        "status": item.status,
        "reminder_at": item.remind_at,
        "due_at": item.due_at,
        "reminder_at_text": format_time(item.remind_at, timezone),
        "due_at_text": format_time(item.due_at, timezone),
    }


def format_list(title: str, items: list[TodoReminder], timezone: ZoneInfo) -> str:
    """格式化待办列表。"""

    if not items:
        return f"当前没有{title}。"
    rows = [f"{title}："]
    for item in items:
        rows.append(
            f"{format_inline(item)}\n"
            f"   状态：{status_label(item.status)}\n"
            f"   提醒时间：{format_time(item.remind_at, timezone)}\n"
            f"   截止时间：{format_time(item.due_at, timezone)}"
        )
    return "\n".join(rows)


def format_detail(item: TodoReminder, timezone: ZoneInfo) -> str:
    """格式化单条待办详情。"""

    return (
        f"{format_inline(item)}\n"
        f"状态：{status_label(item.status)}\n"
        f"提醒时间：{format_time(item.remind_at, timezone)}\n"
        f"截止时间：{format_time(item.due_at, timezone)}"
    )


def format_inline(item: TodoReminder) -> str:
    """格式化单行标题，并在历史记录上展示稳定 ID。"""

    history = f" | 历史 ID: {item.history_id}" if item.status != STATUS_OPEN else ""
    return f"[{item.todo_no}] {truncate(item.title, 80)}{history}"


def format_time(timestamp: int | None, timezone: ZoneInfo) -> str:
    """以用户时区格式化时间戳。"""

    if timestamp is None:
        return "未设置"
    return datetime.fromtimestamp(timestamp, timezone).strftime("%Y-%m-%d %H:%M")


def status_label(status: str) -> str:
    """将内部状态转换为中文状态。"""

    return {
        STATUS_OPEN: "未完成",
        STATUS_DONE: "已完成",
        STATUS_DELETED: "已取消",
    }.get(status, status)


def truncate(text: str, limit: int) -> str:
    """按字符数截断显示标题。"""

    normalized = text.strip()
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "..."
