"""后端托管的 Todo 管理工具。

LLM 只能选择这里白名单中的工具和参数；所有数据库读取、状态校验、
权限校验和持久化都在本模块完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..todo_store import (
    STATUS_DELETED,
    STATUS_DONE,
    STATUS_OPEN,
    TodoReminder,
    TodoReminderDraft,
    TodoStore,
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
    """一次 Todo 工具执行所需的可信运行时上下文。"""

    scope: str
    group_id: str | None
    user_id: str
    now: int
    timezone: ZoneInfo
    max_pending: int = 100
    reject_past_reminder: bool = True
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
        return {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], ToolResult]

    def to_openai_tool(self) -> dict[str, Any]:
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
        super().__init__(result.message)
        self.result = result


class TodoToolExecutor:
    """执行 Todo 管理工具，并保证所有写库操作经过后端校验。"""

    def __init__(self, store: TodoStore, context: TodoToolContext) -> None:
        self.store = store
        self.context = context
        self._specs = _build_tool_specs(self)

    @property
    def tool_definitions(self) -> list[dict[str, Any]]:
        return [spec.to_openai_tool() for spec in self._specs.values()]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        """校验并执行一个工具调用。"""

        spec = self._specs.get(tool_name)
        if spec is None:
            return ToolResult(
                ok=False,
                status="error",
                message=f"拒绝执行未知工具：{tool_name}",
                data={"tool_name": tool_name},
            )
        if not isinstance(arguments, dict):
            return ToolResult(
                ok=False,
                status="error",
                message=f"{tool_name} 的参数必须是 JSON 对象",
                data={"tool_name": tool_name},
            )
        try:
            validate_json_schema(arguments, spec.parameters)
        except ToolValidationError as exc:
            return ToolResult(
                ok=False,
                status="error",
                message=f"{tool_name} 参数不合法：{exc}",
                data={"tool_name": tool_name, "arguments": arguments},
            )
        try:
            return spec.handler(arguments)
        except ToolExecutionStop as exc:
            return exc.result

    def list_todos(self, args: dict[str, Any]) -> ToolResult:
        status = args.get("status") or STATUS_OPEN
        limit = args.get("limit") or 20
        store_status = None if status == "all" else status
        items = self.store.list_by_status(
            self.context.scope,
            self.context.group_id,
            self.context.user_id,
            store_status,
            limit,
        )
        title = {
            STATUS_OPEN: "待办列表",
            STATUS_DONE: "已完成待办",
            STATUS_DELETED: "已取消待办",
            "all": "全部待办",
        }.get(status, "待办列表")
        return ToolResult(
            ok=True,
            status="success",
            message=self._format_list(title, items),
            data={"items": [self._todo_to_dict(item) for item in items]},
        )

    def get_todo(self, args: dict[str, Any]) -> ToolResult:
        number = self._number_from_args(args)
        item = self._resolve_todo(number, None, "查看")
        return ToolResult(
            ok=True,
            status="success",
            message=self._format_detail(item),
            data={"item": self._todo_to_dict(item)},
        )

    def create_todo(self, args: dict[str, Any]) -> ToolResult:
        title = _clean_required_text(args.get("title"), "title")
        content = _clean_optional_text(args.get("content"))
        raw_text = _clean_optional_text(args.get("raw_text")) or self.context.user_text or title
        reminder_at = self._parse_optional_time(args.get("reminder_at"), "reminder_at")
        due_at = self._parse_optional_time(args.get("due_at"), "due_at")
        reminder_text = _clean_optional_text(args.get("reminder_text")) or ""

        if self.context.reject_past_reminder and reminder_at is not None:
            if reminder_at <= self.context.now:
                return ToolResult(
                    ok=False,
                    status="error",
                    message="提醒时间已经过去，待办没有写入",
                    data={},
                )
        if self.store.count_pending(
            self.context.scope,
            self.context.group_id,
            self.context.user_id,
        ) >= self.context.max_pending:
            return ToolResult(
                ok=False,
                status="error",
                message=f"未完成待办已经达到上限 {self.context.max_pending} 条，请先完成或取消一些待办",
                data={},
            )

        drafts = [
            TodoReminderDraft(
                title=title,
                content=content,
                raw_text=raw_text,
                remind_at=reminder_at,
                due_at=due_at,
                reminder_text=reminder_text,
                llm_json={"tool": "create_todo", "arguments": args},
            )
        ]
        created = self.store.create_many(
            self.context.scope,
            self.context.group_id,
            self.context.user_id,
            drafts,
            self.context.now,
        )[0]
        return ToolResult(
            ok=True,
            status="success",
            message=f"已添加待办：{self._format_inline(created)}\n提醒时间：{self._format_time(created.remind_at)}",
            data={"item": self._todo_to_dict(created)},
        )

    def edit_todo(self, args: dict[str, Any]) -> ToolResult:
        number = self._number_from_args(args)
        item = self._resolve_todo(number, (STATUS_OPEN,), "修改")
        updates: dict[str, Any] = {}

        if "title" in args and args.get("title") is not None:
            updates["title"] = _clean_required_text(args.get("title"), "title")
        if "content" in args:
            updates["content"] = _clean_optional_text(args.get("content"))
        if args.get("clear_reminder_at") is True:
            updates["remind_at"] = None
        elif "reminder_at" in args and args.get("reminder_at") is not None:
            updates["remind_at"] = self._parse_optional_time(args.get("reminder_at"), "reminder_at")
        if args.get("clear_due_at") is True:
            updates["due_at"] = None
        elif "due_at" in args and args.get("due_at") is not None:
            updates["due_at"] = self._parse_optional_time(args.get("due_at"), "due_at")
        if "reminder_text" in args and args.get("reminder_text") is not None:
            updates["reminder_text"] = _clean_required_text(args.get("reminder_text"), "reminder_text")

        if not updates:
            return ToolResult(
                ok=False,
                status="clarify",
                message="需要补充要修改的标题、内容或时间",
                data={"number": number},
            )

        updated = self.store.update_fields(item.id, updates, STATUS_OPEN)
        if updated is None:
            return self._status_changed_result(number, "修改")
        return ToolResult(
            ok=True,
            status="success",
            message=f"已修改待办：{self._format_inline(updated)}",
            data={"item": self._todo_to_dict(updated)},
        )

    def shift_todo_time(self, args: dict[str, Any]) -> ToolResult:
        number = self._number_from_args(args)
        item = self._resolve_todo(number, (STATUS_OPEN,), "调整时间")
        field = args["field"]
        direction = args["direction"]
        delta_minutes = args["delta_minutes"]
        delta_seconds = int(delta_minutes) * 60
        if direction == "earlier":
            delta_seconds *= -1

        fields = self._shift_fields(item, field)
        updates: dict[str, Any] = {}
        for field_name in fields:
            current_value = item.remind_at if field_name == "reminder_at" else item.due_at
            if current_value is None:
                continue
            store_field = "remind_at" if field_name == "reminder_at" else "due_at"
            updates[store_field] = current_value + delta_seconds

        if not updates:
            return ToolResult(
                ok=False,
                status="clarify",
                message="这条待办没有任何时间字段，需要用户补充要调整哪个时间",
                data={"number": number},
            )

        updated = self.store.update_fields(item.id, updates, STATUS_OPEN)
        if updated is None:
            return self._status_changed_result(number, "调整时间")
        direction_text = "提前" if direction == "earlier" else "推迟"
        field_text = "、".join(_time_field_label(field_name) for field_name in fields)
        return ToolResult(
            ok=True,
            status="success",
            message=(
                f"已将第 {updated.todo_no} 条待办的{field_text}{direction_text} {delta_minutes} 分钟："
                f"{self._format_inline(updated)}"
            ),
            data={
                "item": self._todo_to_dict(updated),
                "shifted_fields": fields,
                "delta_minutes": delta_minutes,
                "direction": direction,
            },
        )

    def complete_todos(self, args: dict[str, Any]) -> ToolResult:
        numbers = self._numbers_from_args(args)
        items = [self._resolve_todo(number, (STATUS_OPEN,), "完成") for number in numbers]
        completed: list[TodoReminder] = []
        for item in items:
            updated = self.store.complete(item.id)
            if updated is None:
                return self._status_changed_result(item.todo_no, "完成")
            completed.append(updated)
        return ToolResult(
            ok=True,
            status="success",
            message="已完成待办：" + "、".join(self._format_inline(item) for item in completed),
            data={"items": [self._todo_to_dict(item) for item in completed]},
        )

    def cancel_todo(self, args: dict[str, Any]) -> ToolResult:
        number = self._number_from_args(args)
        item = self._resolve_todo(number, (STATUS_OPEN,), "取消")
        canceled = self.store.cancel(item.id)
        if canceled is None:
            return self._status_changed_result(number, "取消")
        return ToolResult(
            ok=True,
            status="success",
            message=f"已取消待办：{self._format_inline(canceled)}",
            data={"item": self._todo_to_dict(canceled)},
        )

    def restore_todos(self, args: dict[str, Any]) -> ToolResult:
        numbers = self._numbers_from_args(args)
        items = [
            self._resolve_todo(number, (STATUS_DONE, STATUS_DELETED), "恢复")
            for number in numbers
        ]
        restored: list[TodoReminder] = []
        for item in items:
            updated = self.store.restore(item.id)
            if updated is None:
                return self._status_changed_result(item.todo_no, "恢复")
            restored.append(updated)
        return ToolResult(
            ok=True,
            status="success",
            message="已恢复待办：" + "、".join(self._format_inline(item) for item in restored),
            data={"items": [self._todo_to_dict(item) for item in restored]},
        )

    def delete_todos(self, args: dict[str, Any]) -> ToolResult:
        numbers = self._numbers_from_args(args)
        items = [self._resolve_todo(number, None, "永久删除") for number in numbers]
        if args.get("confirmed") is not True:
            return ToolResult(
                ok=False,
                status="confirm",
                message=(
                    "永久删除不可恢复。请确认是否永久删除："
                    + "、".join(self._format_inline(item) for item in items)
                ),
                data={"items": [self._todo_to_dict(item) for item in items], "deleted": False},
            )
        for item in items:
            self.store.delete_permanent(item.id)
        return ToolResult(
            ok=True,
            status="success",
            message="已永久删除待办：" + "、".join(f"[{item.todo_no}] {item.title}" for item in items),
            data={"items": [self._todo_to_dict(item) for item in items], "deleted": True},
        )

    def merge_todos(self, args: dict[str, Any]) -> ToolResult:
        numbers = self._numbers_from_args(args)
        if len(numbers) < 2:
            return ToolResult(
                ok=False,
                status="clarify",
                message="合并待办至少需要两个编号",
                data={"numbers": numbers},
            )
        items = [self._resolve_todo(number, (STATUS_OPEN,), "合并") for number in numbers]
        title = _clean_optional_text(args.get("title")) or "；".join(item.title for item in items)
        content_parts = [item.content or item.title for item in items]
        remind_values = [item.remind_at for item in items if item.remind_at is not None]
        due_values = [item.due_at for item in items if item.due_at is not None]
        updates = {
            "title": title,
            "content": "\n".join(content_parts),
            "raw_text": self.context.user_text or "合并待办",
            "reminder_text": "",
            "remind_at": min(remind_values) if remind_values else None,
            "due_at": max(due_values) if due_values else None,
        }
        merged = self.store.update_fields(items[0].id, updates, STATUS_OPEN)
        if merged is None:
            return self._status_changed_result(items[0].todo_no, "合并")
        for item in items[1:]:
            self.store.cancel(item.id)
        return ToolResult(
            ok=True,
            status="success",
            message=f"已合并为待办：{self._format_inline(merged)}",
            data={
                "item": self._todo_to_dict(merged),
                "merged_numbers": numbers,
            },
        )

    def _numbers_from_args(self, args: dict[str, Any]) -> list[int]:
        numbers = args.get("numbers")
        if numbers:
            unique_numbers = list(dict.fromkeys(int(number) for number in numbers))
            if unique_numbers:
                return unique_numbers
        return [self._number_from_args(args)]

    def _number_from_args(self, args: dict[str, Any]) -> int:
        number = args.get("number")
        if number is not None:
            return int(number)
        reference = _clean_optional_text(args.get("reference"))
        if reference in _REFERENCE_WORDS and self.context.last_todo_no is not None:
            return int(self.context.last_todo_no)
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

    def _resolve_todo(
        self,
        number: int,
        statuses: tuple[str, ...] | None,
        action: str,
    ) -> TodoReminder:
        item = self.store.find_by_no(
            self.context.scope,
            self.context.group_id,
            self.context.user_id,
            number,
            statuses,
        )
        if item is not None:
            return item

        existing = self.store.find_by_no(
            self.context.scope,
            self.context.group_id,
            self.context.user_id,
            number,
            None,
        )
        if existing is not None:
            raise ToolExecutionStop(
                ToolResult(
                    ok=False,
                    status="error",
                    message=f"第 {number} 条待办当前状态是{_status_label(existing.status)}，不能{action}",
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

    def _status_changed_result(self, number: int, action: str) -> ToolResult:
        existing = self.store.find_by_no(
            self.context.scope,
            self.context.group_id,
            self.context.user_id,
            number,
            None,
        )
        if existing is not None:
            return ToolResult(
                ok=False,
                status="error",
                message=f"第 {number} 条待办当前状态是{_status_label(existing.status)}，不能{action}",
                data={"number": number, "status": existing.status},
            )
        return ToolResult(
            ok=False,
            status="error",
            message=f"找不到第 {number} 条待办，请先查看待办列表确认编号",
            data={"number": number},
        )

    def _shift_fields(self, item: TodoReminder, field: str) -> list[str]:
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

    def _parse_optional_time(self, value: Any, field_name: str) -> int | None:
        text = _clean_optional_text(value)
        if text is None:
            return None
        parsed = _parse_local_datetime(text, self.context.timezone)
        if field_name == "reminder_at" and self.context.reject_past_reminder:
            if int(parsed.timestamp()) <= self.context.now:
                raise ToolExecutionStop(
                    ToolResult(
                        ok=False,
                        status="error",
                        message="提醒时间已经过去，待办没有写入",
                        data={"field": field_name, "value": text},
                    )
                )
        return int(parsed.timestamp())

    def _todo_to_dict(self, item: TodoReminder) -> dict[str, Any]:
        return {
            "number": item.todo_no,
            "title": item.title,
            "content": item.content,
            "status": item.status,
            "reminder_at": item.remind_at,
            "due_at": item.due_at,
            "reminder_at_text": self._format_time(item.remind_at),
            "due_at_text": self._format_time(item.due_at),
        }

    def _format_list(self, title: str, items: list[TodoReminder]) -> str:
        if not items:
            return f"当前没有{title}。"
        rows = [f"{title}："]
        for item in items:
            rows.append(
                f"{self._format_inline(item)}\n"
                f"   状态：{_status_label(item.status)}\n"
                f"   提醒时间：{self._format_time(item.remind_at)}\n"
                f"   截止时间：{self._format_time(item.due_at)}"
            )
        return "\n".join(rows)

    def _format_detail(self, item: TodoReminder) -> str:
        return (
            f"{self._format_inline(item)}\n"
            f"状态：{_status_label(item.status)}\n"
            f"提醒时间：{self._format_time(item.remind_at)}\n"
            f"截止时间：{self._format_time(item.due_at)}"
        )

    def _format_inline(self, item: TodoReminder) -> str:
        return f"[{item.todo_no}] {_truncate(item.title, 80)}"

    def _format_time(self, timestamp: int | None) -> str:
        if timestamp is None:
            return "未设置"
        return datetime.fromtimestamp(timestamp, self.context.timezone).strftime("%Y-%m-%d %H:%M")


def openai_tool_definitions() -> list[dict[str, Any]]:
    """返回 OpenAI compatible chat/completions 的工具定义。"""

    executor = TodoToolExecutor.__new__(TodoToolExecutor)
    specs = _build_tool_specs(executor)
    return [spec.to_openai_tool() for spec in specs.values()]


def _build_tool_specs(executor: TodoToolExecutor) -> dict[str, ToolSpec]:
    return {
        "list_todos": ToolSpec(
            "list_todos",
            "列出当前用户当前会话范围内的待办。查询类请求优先由程序直接处理，本工具仅供复杂上下文使用。",
            _object_schema(
                {
                    "status": {"type": "string", "enum": [STATUS_OPEN, STATUS_DONE, STATUS_DELETED, "all"]},
                    "limit": {"type": "integer", "minimum": 1},
                },
                required=[],
            ),
            executor.list_todos,
        ),
        "get_todo": ToolSpec(
            "get_todo",
            "按用户可见编号查看一条待办详情。只能传 number 或上下文 reference，不能传数据库 id。",
            _target_schema(required=[]),
            executor.get_todo,
        ),
        "create_todo": ToolSpec(
            "create_todo",
            "创建一条待办。LLM 只负责提供结构化参数，真正写库由后端完成。",
            _object_schema(
                {
                    "title": {"type": "string", "minLength": 1},
                    "content": {"type": ["string", "null"]},
                    "reminder_at": {"type": ["string", "null"]},
                    "due_at": {"type": ["string", "null"]},
                    "reminder_text": {"type": ["string", "null"]},
                    "raw_text": {"type": ["string", "null"]},
                },
                required=["title"],
            ),
            executor.create_todo,
        ),
        "edit_todo": ToolSpec(
            "edit_todo",
            "修改一条未完成待办的标题、内容、提醒时间或截止时间。",
            _object_schema(
                {
                    "number": {"type": ["integer", "null"], "minimum": 1},
                    "reference": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "content": {"type": ["string", "null"]},
                    "reminder_at": {"type": ["string", "null"]},
                    "due_at": {"type": ["string", "null"]},
                    "reminder_text": {"type": ["string", "null"]},
                    "clear_reminder_at": {"type": "boolean"},
                    "clear_due_at": {"type": "boolean"},
                },
                required=[],
            ),
            executor.edit_todo,
        ),
        "shift_todo_time": ToolSpec(
            "shift_todo_time",
            "把一条待办的提醒时间、截止时间或两者按分钟提前或推迟。时间计算只能由后端执行。",
            _object_schema(
                {
                    "number": {"type": ["integer", "null"], "minimum": 1},
                    "reference": {"type": ["string", "null"]},
                    "field": {"type": "string", "enum": ["auto", "due_at", "reminder_at", "both"]},
                    "direction": {"type": "string", "enum": ["later", "earlier"]},
                    "delta_minutes": {"type": "integer", "minimum": 1},
                },
                required=["field", "direction", "delta_minutes"],
            ),
            executor.shift_todo_time,
        ),
        "complete_todos": ToolSpec(
            "complete_todos",
            "完成一个或多个未完成待办。编号必须是用户当前可见编号。",
            _numbers_schema(required=[]),
            executor.complete_todos,
        ),
        "cancel_todo": ToolSpec(
            "cancel_todo",
            "取消一条未完成待办，这是软删除路径，不需要永久删除确认。",
            _target_schema(required=[]),
            executor.cancel_todo,
        ),
        "restore_todos": ToolSpec(
            "restore_todos",
            "恢复一个或多个已完成或已取消待办。",
            _numbers_schema(required=[]),
            executor.restore_todos,
        ),
        "delete_todos": ToolSpec(
            "delete_todos",
            "永久删除一个或多个待办。默认必须确认，confirmed 不为 true 时不能执行删除。",
            _object_schema(
                {
                    "numbers": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1},
                    "number": {"type": ["integer", "null"], "minimum": 1},
                    "reference": {"type": ["string", "null"]},
                    "confirmed": {"type": "boolean"},
                },
                required=[],
            ),
            executor.delete_todos,
        ),
        "merge_todos": ToolSpec(
            "merge_todos",
            "合并多个未完成待办，保留第一个编号并取消其余编号。",
            _object_schema(
                {
                    "numbers": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 2},
                    "title": {"type": ["string", "null"]},
                },
                required=["numbers"],
            ),
            executor.merge_todos,
        ),
    }


def _target_schema(required: list[str]) -> dict[str, Any]:
    return _object_schema(
        {
            "number": {"type": ["integer", "null"], "minimum": 1},
            "reference": {"type": ["string", "null"]},
        },
        required=required,
    )


def _numbers_schema(required: list[str]) -> dict[str, Any]:
    return _object_schema(
        {
            "numbers": {"type": "array", "items": {"type": "integer", "minimum": 1}, "minItems": 1},
            "number": {"type": ["integer", "null"], "minimum": 1},
            "reference": {"type": ["string", "null"]},
        },
        required=required,
    )


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def validate_json_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
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


def _parse_local_datetime(value: str, timezone: ZoneInfo) -> datetime:
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


def _clean_required_text(value: Any, field_name: str) -> str:
    text = _clean_optional_text(value)
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


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none"} or text in {"无", "未设置"}:
        return None
    return text


def _fallback_reminder_text(title: str, reminder_mode: str) -> str:
    return f"该做：{title}"


def _status_label(status: str) -> str:
    return {
        STATUS_OPEN: "未完成",
        STATUS_DONE: "已完成",
        STATUS_DELETED: "已取消",
    }.get(status, status)


def _time_field_label(field_name: str) -> str:
    return "提醒时间" if field_name == "reminder_at" else "截止时间"


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."
