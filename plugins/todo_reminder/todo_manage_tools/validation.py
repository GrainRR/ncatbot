"""参数 Schema、文本和时间校验。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..todo_store import ReminderTimeValidationError, validate_remind_at
from .contracts import TodoToolContext, ToolExecutionStop, ToolResult, ToolValidationError


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """构建拒绝额外字段的对象 Schema。"""

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def target_schema(required: list[str]) -> dict[str, Any]:
    """构建以未完成待办序号为目标的 Schema。"""

    return object_schema(
        {
            "number": {"type": ["integer", "null"], "minimum": 1},
            "reference": {"type": ["string", "null"]},
        },
        required,
    )


def numbers_schema(required: list[str]) -> dict[str, Any]:
    """构建单个或多个未完成待办序号的 Schema。"""

    return object_schema(
        {
            "numbers": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
            },
            "number": {"type": ["integer", "null"], "minimum": 1},
            "reference": {"type": ["string", "null"]},
        },
        required,
    )


def history_targets_schema() -> dict[str, Any]:
    """构建只能用稳定历史 ID 指定记录的 Schema。"""

    return object_schema(
        {
            "history_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
            },
            "history_id": {"type": ["string", "null"], "minLength": 1},
            "reminder_at": {"type": ["string", "null"]},
        },
        required=[],
    )


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """递归校验工具参数使用的最小 JSON Schema 子集。

    Args:
        value: 待校验的 LLM 参数或其嵌套值。
        schema: 注册表声明的 Schema，支持对象、数组、联合类型、枚举、必填项、
            `additionalProperties`、最小值和最小长度。
        path: 当前值的 JSONPath 风格位置，用于返回可定位的错误消息。

    Raises:
        ToolValidationError: 类型、枚举、必填字段、额外字段、数组大小或下界约束
            任一不满足时。
    """

    expected_type = schema.get("type")
    if expected_type is not None:
        expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_matches_json_type(value, candidate) for candidate in expected_types):
            raise ToolValidationError(f"{path} 类型应为 {expected_type}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolValidationError(f"{path} 只能是 {schema['enum']}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for field in schema.get("required", []):
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
        minimum = schema.get("minItems")
        if minimum is not None and len(value) < int(minimum):
            raise ToolValidationError(f"{path} 至少需要 {minimum} 项")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, f"{path}[{index}]")

    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < int(minimum):
            raise ToolValidationError(f"{path} 必须大于等于 {minimum}")
    if isinstance(value, str):
        minimum = schema.get("minLength")
        if minimum is not None and len(value.strip()) < int(minimum):
            raise ToolValidationError(f"{path} 不能为空")


def clean_optional_text(value: Any) -> str | None:
    """归一化可选文本字段。"""

    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none"} or text in {"无", "未设置"}:
        return None
    return text


def clean_required_text(value: Any, field_name: str) -> str:
    """归一化必填文本字段。"""

    text = clean_optional_text(value)
    if text:
        return text
    raise ToolExecutionStop(
        ToolResult(False, "error", f"{field_name} 不能为空", {"field": field_name})
    )


def parse_optional_time(value: Any, field_name: str, context: TodoToolContext) -> int | None:
    """解析可选时间文本，并对提醒时间执行唯一业务校验入口。

    Args:
        value: LLM 提供的 ISO 或本地时间文本；空、`null`、`none`、`无` 与
            `未设置` 都视为未设置。
        field_name: 工具字段名；仅 `reminder_at` 需要校验严格晚于当前时间。
        context: 含可信时区、当前时间及 `reject_past_reminder` 开关的上下文。

    Returns:
        Unix 秒级时间戳，或未设置时的 `None`。

    Raises:
        ToolExecutionStop: 文本格式非法或提醒时间不满足未来时间规则时。
    """

    text = clean_optional_text(value)
    if text is None:
        return None
    parsed = parse_local_datetime(text, context.timezone)
    timestamp = int(parsed.timestamp())
    if field_name == "reminder_at":
        try:
            validate_remind_at(timestamp, context.now, context.reject_past_reminder)
        except ReminderTimeValidationError as exc:
            raise ToolExecutionStop(reminder_validation_result(exc)) from exc
    return timestamp


def parse_local_datetime(value: str, timezone: ZoneInfo) -> datetime:
    """解析带或不带时区的用户时间。"""

    normalized = value.strip().replace("T", " ")
    if normalized.endswith("Z") or "+" in normalized[10:] or "-" in normalized[10:]:
        try:
            return datetime.fromisoformat(normalized.replace("Z", "+00:00")).astimezone(timezone)
        except ValueError as exc:
            raise _time_format_error(value) from exc
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone)
        except ValueError:
            continue
    raise _time_format_error(value)


def reminder_validation_result(exc: ReminderTimeValidationError) -> ToolResult:
    """转换统一提醒时间异常。"""

    return ToolResult(
        False,
        "error",
        "提醒时间必须晚于当前时间，待办没有写入",
        {"code": "remind_at_not_future", "remind_at": exc.remind_at, "now": exc.now},
    )


def fallback_reminder_text(title: str) -> str:
    """生成简洁模式的默认提醒文案。"""

    return f"该做：{title}"


def _time_format_error(value: str) -> ToolExecutionStop:
    return ToolExecutionStop(
        ToolResult(
            False,
            "error",
            "时间格式不正确，需要 YYYY-MM-DD HH:MM:SS",
            {"value": value},
        )
    )


def _matches_json_type(value: Any, expected_type: str) -> bool:
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
