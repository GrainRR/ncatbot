"""Todo 管理工具的共享类型和公共 helper。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..todo_store import (
    ReminderTimeValidationError,
    STATUS_DELETED,
    STATUS_DONE,
    STATUS_OPEN,
    TodoReminder,
    TodoStore,
    validate_remind_at,
)


TODO_TOOL_NAMES = {
    "list_todos",
    "get_todo",
    "create_todo",
    "edit_todo",
    "shift_todo_time",
    "complete_todos",
    "cancel_todo",
    "restore_todos",
    "delete_todos",
    "merge_todos",
}

_REFERENCE_WORDS = {
    "刚才那个",
    "刚才那条",
    "刚刚那个",
    "刚刚那条",
    "上一个",
    "上一条",
    "前一个",
    "前一条",
}


@dataclass(frozen=True)
class TodoToolContext:
    """一次 Todo 工具执行所需的可信运行时上下文。

    这些字段由程序路由层生成，不能由 LLM 自行指定。工具执行时只信任
    这里的范围、用户、时间和提醒风格配置。
    """

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
class ToolResult:
    """结构化工具执行结果。"""

    ok: bool
    status: str
    message: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """转换为可序列化的字典。

        Returns:
            包含执行状态、用户可见消息和结构化数据的字典。
        """

        return {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }


@dataclass(frozen=True)
class ToolSpec:
    """后端工具定义。

    包含暴露给 LLM 的工具描述、JSON schema，以及最终由后端调用的
    执行函数。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult]

    def to_openai_tool(self) -> dict[str, Any]:
        """转换为 OpenAI compatible tools 参数格式。

        Returns:
            可直接传给 chat/completions `tools` 字段的工具定义。
        """

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolValidationError(Exception):
    """工具参数 schema 校验失败。"""


class ToolExecutionStop(Exception):
    """工具执行遇到错误、确认或澄清时短路。"""

    def __init__(self, result: ToolResult) -> None:
        """保存需要直接返回给 Tool Loop 的工具结果。

        Args:
            result: 已经结构化的错误、确认或澄清结果。
        """

        super().__init__(result.message)
        self.result = result


def numbers_from_args(args: dict[str, Any], context: TodoToolContext) -> list[int]:
    """从工具参数中解析一个或多个用户可见编号。

    Args:
        args: 已通过 schema 校验的工具参数。
        context: 程序路由层生成的可信执行上下文。

    Returns:
        去重后的正整数编号列表。
    """

    numbers = args.get("numbers")
    if numbers:
        unique_numbers = list(dict.fromkeys(int(number) for number in numbers))
        if unique_numbers:
            return unique_numbers
    return [number_from_args(args, context)]


def number_from_args(args: dict[str, Any], context: TodoToolContext) -> int:
    """从工具参数中解析单个用户可见编号。

    支持显式 `number`、数字字符串 `reference`，以及“刚才那个”等
    由程序上下文记录的引用词。

    Args:
        args: 已通过 schema 校验的工具参数。
        context: 程序路由层生成的可信执行上下文。

    Returns:
        用户当前可见待办编号。

    Raises:
        ToolExecutionStop: 缺少可解析编号时抛出澄清结果。
    """

    number = args.get("number")
    if number is not None:
        return int(number)
    reference = clean_optional_text(args.get("reference"))
    if reference in _REFERENCE_WORDS and context.last_todo_no is not None:
        return int(context.last_todo_no)
    if reference and reference.isdigit():
        return int(reference)
    raise ToolExecutionStop(
        ToolResult(
            ok=False,
            status="clarify",
            message="请说明要操作第几条待办",
            data={"reference": reference},
        )
    )


def resolve_todo(
    store: TodoStore,
    context: TodoToolContext,
    number: int,
    statuses: tuple[str, ...] | None,
    action: str,
) -> TodoReminder:
    """在可信上下文范围内解析待办并校验状态。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        number: 用户当前可见编号。
        statuses: 允许的待办状态；传入 None 时允许所有状态。
        action: 当前业务动作名称，用于生成错误提示。

    Returns:
        匹配到且状态合法的待办。

    Raises:
        ToolExecutionStop: 目标不存在或当前状态不允许执行该动作。
    """

    item = store.find_by_no(
        context.scope,
        context.group_id,
        context.user_id,
        number,
        statuses,
    )
    if item is not None:
        return item

    existing = store.find_by_no(
        context.scope,
        context.group_id,
        context.user_id,
        number,
        None,
    )
    if existing is not None:
        raise ToolExecutionStop(
            ToolResult(
                ok=False,
                status="error",
                message=f"第 {number} 条待办当前状态是{status_label(existing.status)}，不能{action}",
                data={"number": number, "status": existing.status},
            )
        )
    raise ToolExecutionStop(
        ToolResult(
            ok=False,
            status="error",
            message=f"找不到第 {number} 条待办，请先查看待办列表确认编号",
            data={"number": number},
        )
    )


def status_changed_result(
    store: TodoStore,
    context: TodoToolContext,
    number: int,
    action: str,
) -> ToolResult:
    """生成并发状态变化后的失败结果。

    Args:
        store: Todo 存储层实例。
        context: 程序路由层生成的可信执行上下文。
        number: 用户当前可见编号。
        action: 当前业务动作名称，用于生成错误提示。

    Returns:
        描述目标不存在或状态已经变化的结构化结果。
    """

    existing = store.find_by_no(
        context.scope,
        context.group_id,
        context.user_id,
        number,
        None,
    )
    if existing is not None:
        return ToolResult(
            ok=False,
            status="error",
            message=f"第 {number} 条待办当前状态是{status_label(existing.status)}，不能{action}",
            data={"number": number, "status": existing.status},
        )
    return ToolResult(
        ok=False,
        status="error",
        message=f"找不到第 {number} 条待办，请先查看待办列表确认编号",
        data={"number": number},
    )


def shift_fields(item: TodoReminder, field: str) -> list[str]:
    """解析需要调整的时间字段。

    Args:
        item: 待调整的未完成待办。
        field: LLM 传入的字段选择，取值为 `auto`、`due_at`、
            `reminder_at` 或 `both`。

    Returns:
        需要更新的工具层字段名列表。

    Raises:
        ToolExecutionStop: 目标没有对应时间字段，或 `auto` 无法消歧。
    """

    if field == "both":
        if item.remind_at is None and item.due_at is None:
            raise ToolExecutionStop(
                ToolResult(
                    ok=False,
                    status="clarify",
                    message="这条待办没有任何时间字段，需要用户补充要调整哪个时间",
                    data={"number": item.todo_no},
                )
            )
        return ["reminder_at", "due_at"]
    if field == "reminder_at":
        if item.remind_at is None:
            raise ToolExecutionStop(
                ToolResult(
                    ok=False,
                    status="clarify",
                    message="这条待办没有提醒时间，需要用户补充新的提醒时间",
                    data={"number": item.todo_no},
                )
            )
        return ["reminder_at"]
    if field == "due_at":
        if item.due_at is None:
            raise ToolExecutionStop(
                ToolResult(
                    ok=False,
                    status="clarify",
                    message="这条待办没有截止时间，需要用户补充新的截止时间",
                    data={"number": item.todo_no},
                )
            )
        return ["due_at"]

    has_reminder = item.remind_at is not None
    has_due = item.due_at is not None
    if not has_reminder and not has_due:
        raise ToolExecutionStop(
            ToolResult(
                ok=False,
                status="clarify",
                message="这条待办没有任何时间字段，需要用户补充要调整哪个时间",
                data={"number": item.todo_no},
            )
        )
    if has_reminder and has_due:
        raise ToolExecutionStop(
            ToolResult(
                ok=False,
                status="clarify",
                message="这条待办同时有提醒时间和截止时间，请说明要调整提醒时间、截止时间还是都调整",
                data={"number": item.todo_no},
            )
        )
    return ["reminder_at"] if has_reminder else ["due_at"]


def parse_optional_time(
    value: Any,
    field_name: str,
    context: TodoToolContext,
) -> int | None:
    """解析可选时间参数并应用提醒时间业务校验。

    Args:
        value: LLM 传入的时间文本或空值。
        field_name: 当前解析的工具字段名。
        context: 程序路由层生成的可信执行上下文。

    Returns:
        Unix 秒级时间戳；空值返回 None。

    Raises:
        ToolExecutionStop: 时间格式非法，或提醒时间早于当前时间。
    """

    text = clean_optional_text(value)
    if text is None:
        return None
    parsed = _parse_local_datetime(text, context.timezone)
    timestamp = int(parsed.timestamp())
    if field_name == "reminder_at":
        try:
            validate_remind_at(timestamp, context.now, context.reject_past_reminder)
        except ReminderTimeValidationError as exc:
            raise ToolExecutionStop(
                ToolResult(
                    ok=False,
                    status="error",
                    message="提醒时间必须晚于当前时间，待办没有写入",
                    data={"code": "remind_at_not_future", "remind_at": exc.remind_at, "now": exc.now},
                )
            ) from exc
    return timestamp


def todo_to_dict(item: TodoReminder, timezone: ZoneInfo) -> dict[str, Any]:
    """把待办记录转换为工具结果中的结构化数据。

    Args:
        item: 待办记录。
        timezone: 用于格式化时间的时区。

    Returns:
        只暴露用户可见编号和业务字段的字典，不包含数据库内部 ID。
    """

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
    """格式化待办列表回复。

    Args:
        title: 列表标题。
        items: 待展示的待办列表。
        timezone: 用于格式化时间的时区。

    Returns:
        可直接发送给用户的列表文本。
    """

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
    """格式化单条待办详情。

    Args:
        item: 待办记录。
        timezone: 用于格式化时间的时区。

    Returns:
        可直接发送给用户的详情文本。
    """

    return (
        f"{format_inline(item)}\n"
        f"状态：{status_label(item.status)}\n"
        f"提醒时间：{format_time(item.remind_at, timezone)}\n"
        f"截止时间：{format_time(item.due_at, timezone)}"
    )


def format_inline(item: TodoReminder) -> str:
    """格式化待办的单行标题。

    Args:
        item: 待办记录。

    Returns:
        形如 `[1] 标题` 的展示文本。
    """

    history = f" | 历史 ID: {item.history_id}" if item.status != STATUS_OPEN else ""
    return f"[{item.todo_no}] {truncate(item.title, 80)}{history}"


def format_time(timestamp: int | None, timezone: ZoneInfo) -> str:
    """按工具上下文时区格式化时间戳。

    Args:
        timestamp: Unix 秒级时间戳；未设置时传入 None。
        timezone: 用于格式化时间的时区。

    Returns:
        本地时间文本，或 `未设置`。
    """

    if timestamp is None:
        return "未设置"
    return datetime.fromtimestamp(timestamp, timezone).strftime("%Y-%m-%d %H:%M")


def target_schema(required: list[str]) -> dict[str, Any]:
    """构建单目标工具的 JSON schema。

    Args:
        required: 必填字段名列表。

    Returns:
        允许 `number` 或 `reference` 的对象 schema。
    """

    return object_schema(
        {
            "number": {"type": ["integer", "null"], "minimum": 1},
            "reference": {"type": ["string", "null"]},
        },
        required=required,
    )


def numbers_schema(required: list[str]) -> dict[str, Any]:
    """构建多目标工具的 JSON schema。

    Args:
        required: 必填字段名列表。

    Returns:
        允许 `numbers`、`number` 或 `reference` 的对象 schema。
    """

    return object_schema(
        {
            "numbers": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1},
            "number": {"type": ["integer", "null"], "minimum": 1},
            "reference": {"type": ["string", "null"]},
        },
        required=required,
    )


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """构建禁止额外字段的对象 schema。

    Args:
        properties: JSON schema properties 定义。
        required: 必填字段名列表。

    Returns:
        `additionalProperties=False` 的对象 schema。
    """

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """按项目内最小 JSON schema 子集校验工具参数。

    Args:
        value: 待校验的参数值。
        schema: 工具参数 schema。
        path: 当前校验路径，用于生成错误提示。

    Raises:
        ToolValidationError: 参数类型、枚举、必填字段、额外字段或范围不满足 schema。
    """

    expected_type = schema.get("type")
    if expected_type is not None:
        expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_matches_json_type(value, item) for item in expected_types):
            raise ToolValidationError(f"{path} 类型应为 {expected_type}")

    if "enum" in schema and value not in schema["enum"]:
        raise ToolValidationError(f"{path} 只能是 {schema['enum']}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for field in required:
            if field not in value:
                raise ToolValidationError(f"{path}.{field} 是必填字段")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise ToolValidationError(f"{path} 包含不允许的字段 {sorted(unknown)}")
        for field, field_value in value.items():
            if field in properties:
                validate_json_schema(field_value, properties[field], f"{path}.{field}")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < int(min_items):
            raise ToolValidationError(f"{path} 至少需要 {min_items} 项")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, f"{path}[{index}]")

    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < int(minimum):
            raise ToolValidationError(f"{path} 必须大于等于 {minimum}")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if min_length is not None and len(value.strip()) < int(min_length):
            raise ToolValidationError(f"{path} 不能为空")


def clean_required_text(value: Any, field_name: str) -> str:
    """清洗必填文本字段。

    Args:
        value: LLM 传入的字段值。
        field_name: 字段名，用于生成错误提示。

    Returns:
        去掉首尾空白后的文本。

    Raises:
        ToolExecutionStop: 字段为空或等价于空值。
    """

    text = clean_optional_text(value)
    if not text:
        raise ToolExecutionStop(
            ToolResult(
                ok=False,
                status="error",
                message=f"{field_name} 不能为空",
                data={"field": field_name},
            )
        )
    return text


def clean_optional_text(value: Any) -> str | None:
    """清洗可选文本字段。

    Args:
        value: LLM 传入的字段值。

    Returns:
        去掉首尾空白后的文本；空值、`null`、`none`、`无` 或 `未设置`
        返回 None。
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none"} or text in {"无", "未设置"}:
        return None
    return text


def fallback_reminder_text(title: str) -> str:
    """生成简洁模式的默认提醒文案。

    Args:
        title: 待办标题。

    Returns:
        可用于到点提醒的简洁文案。
    """

    return f"该做：{title}"


def status_label(status: str) -> str:
    """把内部状态值转换为用户可读文案。

    Args:
        status: 内部状态值。

    Returns:
        用户可读的中文状态名。
    """

    return {
        STATUS_OPEN: "未完成",
        STATUS_DONE: "已完成",
        STATUS_DELETED: "已取消",
    }.get(status, status)


def time_field_label(field_name: str) -> str:
    """把工具层时间字段名转换为用户可读文案。

    Args:
        field_name: 工具层时间字段名。

    Returns:
        `提醒时间` 或 `截止时间`。
    """

    return "提醒时间" if field_name == "reminder_at" else "截止时间"


def truncate(text: str, limit: int) -> str:
    """按字符数截断文本。

    Args:
        text: 原始文本。
        limit: 最大字符数。

    Returns:
        不超过指定长度的文本。
    """

    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _matches_json_type(value: Any, expected_type: str) -> bool:
    """判断值是否匹配 JSON schema 类型。

    Args:
        value: 待检查的值。
        expected_type: JSON schema 类型名。

    Returns:
        类型匹配时返回 True，否则返回 False。
    """

    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return False


def _parse_local_datetime(value: str, timezone: ZoneInfo) -> datetime:
    """解析 LLM 传入的本地时间或带时区时间。

    Args:
        value: 时间文本，支持 `YYYY-MM-DD HH:MM`、
            `YYYY-MM-DD HH:MM:SS` 和 ISO 带时区格式。
        timezone: 未显式带时区时使用的本地时区。

    Returns:
        带时区信息的 datetime。

    Raises:
        ToolExecutionStop: 时间文本无法解析时抛出结构化错误。
    """

    normalized = value.strip().replace("T", " ")
    if normalized.endswith("Z") or "+" in normalized[10:] or "-" in normalized[10:]:
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolExecutionStop(
                ToolResult(
                    ok=False,
                    status="error",
                    message="时间格式不正确，需要 YYYY-MM-DD HH:MM:SS",
                    data={"value": value},
                )
            ) from exc
        return parsed.astimezone(timezone)

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone)
        except ValueError:
            continue
    raise ToolExecutionStop(
        ToolResult(
            ok=False,
            status="error",
            message="时间格式不正确，需要 YYYY-MM-DD HH:MM:SS",
            data={"value": value},
        )
    )
