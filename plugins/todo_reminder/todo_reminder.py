"""待办提醒插件入口。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ncatbot.core import registrar
from ncatbot.event.qq import GroupMessageEvent, PrivateMessageEvent
from ncatbot.plugin import NcatBotPlugin

from .llm import TodoToolLoop
from .todo_manage_tools import TodoToolContext
from .todo_store import (
    MODE_CATGIRL,
    MODE_CONCISE,
    STATUS_DONE,
    TODO_DB_FILENAME,
    TodoReminder,
    TodoStore,
    parse_pending_target_number,
)


SCOPE_GROUP = "group"
SCOPE_PRIVATE = "private"
TODO_ROUTE_NONE = "none"
TODO_ROUTE_PENDING = "pending"
TODO_ROUTE_COMPLETED = "completed"
TODO_ROUTE_TOOL_LOOP = "tool_loop"

_PENDING_QUERY_TEXTS = {
    "/todo",
    "todo",
    "查看待办",
    "待办列表",
    "查看待办列表",
    "查看未完成",
    "查看未完成待办",
}
_COMPLETED_QUERY_TEXTS = {
    "/todo done",
    "/todo 已完成",
    "查看已完成",
    "查看已完成待办",
    "已完成待办",
}
_MODE_CATGIRL_TEXTS = {
    "猫娘模式",
    "待办猫娘模式",
    "提醒猫娘模式",
    "切换猫娘模式",
    "设置猫娘模式",
}
_MODE_CONCISE_TEXTS = {
    "简洁模式",
    "待办简洁模式",
    "提醒简洁模式",
    "切换简洁模式",
    "设置简洁模式",
}
_TODO_WRITE_KEYWORDS = (
    "新增",
    "添加",
    "创建",
    "新建",
    "加个",
    "加一条",
    "完成",
    "修改",
    "更改",
    "改成",
    "改为",
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
    "提醒",
    "提醒我",
)
_TODO_REFERENCE_WORDS = (
    "第",
    "条",
    "上一条",
    "前一条",
    "刚才那个",
    "刚才那条",
    "刚刚那个",
    "刚刚那条",
    "提醒",
    "时间",
    "分钟",
    "小时",
)
_CHINESE_NUMBER_CHARS = set("一二三四五六七八九十两")
_TODO_CREATE_PREFIXES = (
    "新增",
    "添加",
    "创建",
    "新建",
    "加个",
    "加一条",
)
_GROUP_TODO_TOGGLE_TEXTS = {
    "#待办 开启": True,
    "#待办 关闭": False,
}


class TodoReminderPlugin(NcatBotPlugin):
    """基于 LLM 解析自然语言，并在指定时间发送待办提醒。"""

    name = "todo_reminder"
    version = "0.1.0"
    author = "Li"
    description = "LLM 待办提醒插件"

    store: TodoStore
    tool_loop: TodoToolLoop
    _last_todo_numbers: dict[str, int]
    _checking_due: bool

    async def on_load(self):
        """插件加载时初始化配置、数据库、LLM 解析器和定时扫描任务。"""

        configured_max_pending = None
        raw_config = getattr(self, "config", None)
        if isinstance(raw_config, dict):
            configured_max_pending = raw_config.get("max_pending_todos_per_user")
            if configured_max_pending is None:
                configured_max_pending = raw_config.get("max_pending_todos_per_scope")
        self._configured_max_pending = configured_max_pending
        self.init_defaults(
            {
                "llm_api_base": "",
                "llm_api_url": "",
                "llm_api_key": "",
                "llm_api_key_env": "TODO_REMINDER_LLM_API_KEY",
                "llm_model": "",
                "llm_timeout_seconds": 30,
                "timezone": "Asia/Shanghai",
                "default_reminder_mode": MODE_CONCISE,
                "reminder_check_interval": "60s",
                "max_pending_todos_per_user": 100,
                "max_pending_todos_per_scope": 100,
                "max_due_reminders_per_check": 20,
                "reject_past_reminder": True,
                "permanent_delete_confirmation_ttl_seconds": 300,
            }
        )
        self.store = TodoStore(self.workspace / TODO_DB_FILENAME)
        self.store.init()
        self.tool_loop = TodoToolLoop(self.config, self.store)
        self._last_todo_numbers = {}
        self._checking_due = False
        self.add_scheduled_task(
            "check_due_todos",
            self.get_config("reminder_check_interval", "60s"),
        )
        self.logger.info("待办提醒数据库已就绪: %s", self.store.db_path)

    @registrar.qq.on_group_message()
    async def route_group_todo_message(self, event: GroupMessageEvent):
        """把普通群消息中的 Todo 查询和写操作路由到确定性路径或 Tool Loop。

        Args:
            event: 收到的群消息事件。
        """

        text = " ".join(_event_text(event).strip().split())
        # 判断群聊待办功能启用状态
        enabled = _GROUP_TODO_TOGGLE_TEXTS.get(text)
        if enabled is not None:
            await self._handle_group_todo_toggle(event, enabled)
            return
        store = getattr(self, "store", None)
        if store is None or not store.is_group_todo_enabled(str(event.group_id)):
            return
        await self._route_todo_message(event, *self._group_context(event))

    @registrar.qq.on_private_message()
    async def route_private_todo_message(self, event: PrivateMessageEvent):
        """把普通私聊消息中的 Todo 查询和写操作路由到确定性路径或 Tool Loop。

        Args:
            event: 收到的私聊消息事件。
        """

        await self._route_todo_message(event, *self._private_context(event))

    def _group_context(self, event: GroupMessageEvent) -> tuple[str, str | None, str]:
        """生成群聊命令使用的待办范围参数。

        Args:
            event: 触发命令的群消息事件。

        Returns:
            依次返回范围类型、群号和用户 QQ 号。
        """

        return SCOPE_GROUP, str(event.group_id), _event_user_id(event)

    def _private_context(self, event: PrivateMessageEvent) -> tuple[str, str | None, str]:
        """生成私聊命令使用的待办范围参数。

        Args:
            event: 触发命令的私聊消息事件。

        Returns:
            依次返回范围类型、空群号和用户 QQ 号。
        """

        return SCOPE_PRIVATE, None, _event_user_id(event)

    async def _handle_group_todo_toggle(self, event: GroupMessageEvent, enabled: bool) -> None:
        """处理群待办开关，权限查询失败或权限不足时不写入配置。"""

        permission = await self._group_manager_permission(event)
        if permission is None:
            await event.reply("无法查询群成员权限，群待办开关未修改")
            return
        if not permission:
            await event.reply("只有群主和管理员可以切换群待办开关")
            return
        self.store.set_group_todo_enabled(str(event.group_id), enabled, self._now())
        state = "开启" if enabled else "关闭"
        await event.reply(f"群待办已{state}")

    async def _group_manager_permission(self, event: GroupMessageEvent) -> bool | None:
        """查询群主/管理员权限；查询失败返回 None。"""

        try:
            member_info = await self.api.qq.query.get_group_member_info(
                group_id=event.group_id,
                user_id=_event_user_id(event),
            )
        except Exception as exc:
            self.logger.exception("查询群待办开关权限失败: %s", exc)
            return None
        return getattr(member_info, "role", None) in ("owner", "admin")

    async def _list_todos(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
    ) -> None:
        """回复当前范围内的未完成待办列表。

        Args:
            event: 触发命令的消息事件，用于回复用户。
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
        """

        items = self.store.list_pending(scope, group_id, user_id)
        await event.reply(self._format_pending_list(items))

    async def _list_completed_todos(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
    ) -> None:
        """回复当前范围内的已完成待办列表。

        Args:
            event: 触发命令的消息事件，用于回复用户。
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
        """

        items = self.store.list_completed(scope, group_id, user_id)
        await event.reply(self._format_todo_list("已完成待办", items))

    async def _switch_mode(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
        mode: str,
    ) -> None:
        """切换当前用户在当前范围内的提醒文案模式。

        Args:
            event: 触发命令的消息事件，用于回复用户。
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            mode: 提醒模式，取值为 `concise` 或 `catgirl`。
        """

        self.store.set_mode(scope, group_id, user_id, mode, self._now())
        mode_name = "猫娘" if mode == MODE_CATGIRL else "简洁"
        await event.reply(f"已切换为{mode_name}模式，之后到点提醒会使用{mode_name}文案")

    async def _route_todo_message(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
    ) -> None:
        """普通消息的 Todo 程序路由。

        确定性查询请求在这里直接处理；写操作和时间调整类自然语言请求进入
        Tool Loop；不属于 Todo 的消息直接忽略。

        Args:
            event: 收到的群聊或私聊消息事件。
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
        """

        text = _event_text(event)
        normalized = " ".join(text.strip().split())
        if normalized in _MODE_CATGIRL_TEXTS:
            await self._switch_mode(event, scope, group_id, user_id, MODE_CATGIRL)
            return
        if normalized in _MODE_CONCISE_TEXTS:
            await self._switch_mode(event, scope, group_id, user_id, MODE_CONCISE)
            return

        route = classify_todo_route(text)
        if route == TODO_ROUTE_NONE:
            return
        if route == TODO_ROUTE_PENDING:
            await self._list_todos(event, scope, group_id, user_id)
            return
        if route == TODO_ROUTE_COMPLETED:
            await self._list_completed_todos(event, scope, group_id, user_id)
            return
        await self._run_todo_tool_loop(event, scope, group_id, user_id, text)

    async def _run_todo_tool_loop(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
        user_text: str,
    ) -> None:
        """执行 Todo Tool Loop 并基于真实工具结果回复。

        Args:
            event: 收到的群聊或私聊消息事件。
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            user_text: 交给 Tool Loop 的规范化用户文本。
        """

        context = self._tool_context(scope, group_id, user_id, user_text)
        try:
            response = await self.tool_loop.run(user_text, context)
        except Exception as exc:
            self.logger.exception(
                "Todo Tool Loop 失败: scope=%s group_id=%s user_id=%s raw=%s error=%s",
                scope,
                group_id,
                user_id,
                _truncate(user_text, 100),
                exc,
            )
            await event.reply(f"处理待办失败，数据库未变更：{exc}")
            return
        self._remember_tool_results(scope, group_id, user_id, response.tool_results)
        await event.reply(response.message)

    def _tool_context(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        user_text: str,
    ) -> TodoToolContext:
        """构造传给后端工具的可信上下文。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            user_text: 交给 Tool Loop 的用户文本。

        Returns:
            后端工具执行器使用的可信上下文。
        """

        return TodoToolContext(
            scope=scope,
            group_id=group_id,
            user_id=user_id,
            now=self._now(),
            timezone=self._timezone(),
            max_pending=self._max_pending_limit(),
            reject_past_reminder=bool(self.get_config("reject_past_reminder", True)),
            permanent_delete_confirmation_ttl_seconds=_positive_int(
                self.get_config("permanent_delete_confirmation_ttl_seconds"), 300
            ),
            last_todo_no=self._last_todo_numbers.get(self._context_key(scope, group_id, user_id)),
            reminder_mode=self._reminder_mode(scope, group_id, user_id),
            user_text=user_text,
        )

    def _remember_tool_results(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        results: list[Any],
    ) -> None:
        """记录最后一次工具操作涉及的用户可见编号。

        支持用户后续用“刚才那个”“上一条”等引用词操作同一条待办。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            results: Tool Loop 返回的结构化工具结果列表。
        """

        key = self._context_key(scope, group_id, user_id)
        for result in results:
            data = getattr(result, "data", {}) or {}
            item = data.get("item")
            if isinstance(item, dict) and isinstance(item.get("number"), int):
                self._last_todo_numbers[key] = int(item["number"])
            for item in data.get("items") or []:
                if isinstance(item, dict) and isinstance(item.get("number"), int):
                    self._last_todo_numbers[key] = int(item["number"])

    @staticmethod
    def _context_key(scope: str, group_id: str | None, user_id: str) -> str:
        """生成记录最近操作编号的上下文键。

        Args:
            scope: 待办来源范围。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。

        Returns:
            可作为字典键使用的稳定三元组。
        """

        return str(user_id)

    async def check_due_todos(self) -> None:
        """定时扫描到期待办，并按来源发送群聊或私聊提醒。

        发送成功后才标记为已提醒并自动软删除，避免消息发送失败时丢失提醒。
        """

        if self._checking_due:
            return
        self._checking_due = True
        try:
            due_items = self.store.due_pending(
                self._now(),
                _positive_int(self.get_config("max_due_reminders_per_check"), 20),
            )
            for item in due_items:
                try:
                    await self._send_reminder(item)
                    # 只有发送成功后才标记并自动软删除，避免发送失败后丢提醒。
                    self.store.mark_reminded(item.id, self._now(), item.user_id)
                    self.logger.info(
                        "待办提醒已发送并自动删除: id=%s todo_no=%s scope=%s group_id=%s user_id=%s",
                        item.id,
                        item.todo_no,
                        item.scope,
                        item.group_id,
                        item.user_id,
                    )
                except Exception as exc:
                    self.logger.exception("发送待办提醒失败 todo_id=%s: %s", item.id, exc)
        finally:
            self._checking_due = False

    async def _send_reminder(self, item: TodoReminder) -> bool:
        """发送单条到期待办提醒。

        Args:
            item: 已到期且尚未提醒的待办记录。
        """

        text = item.reminder_text or f"待办提醒：{item.title}"

        await self.api.qq.send_private_text(item.user_id, text)
        return True

    def _reminder_mode(self, scope: str, group_id: str | None, user_id: str) -> str:
        """获取当前提醒展示风格。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。

        Returns:
            当前范围内的提醒风格；未设置或配置非法时返回 `concise`。
        """

        mode = self.store.get_mode(scope, group_id, user_id)
        if mode not in {MODE_CONCISE, MODE_CATGIRL}:
            configured = str(self.get_config("default_reminder_mode", MODE_CONCISE) or MODE_CONCISE)
            return configured if configured in {MODE_CONCISE, MODE_CATGIRL} else MODE_CONCISE
        return mode

    def _max_pending_limit(self) -> int:
        """读取每用户待办上限，并兼容旧的每范围配置键。"""

        configured = getattr(self, "_configured_max_pending", None)
        if configured is None:
            configured = self.get_config("max_pending_todos_per_user", None)
        if configured is None:
            configured = self.get_config("max_pending_todos_per_scope", 100)
        return _positive_int(configured, 100)

    def _resolve_target(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        target: str,
    ) -> TodoReminder | None:
        """把用户输入的待办序号解析为当前范围内的一条未完成待办。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            target: 用户输入的范围内待办序号。

        Returns:
            找到时返回待办记录，否则返回 None。
        """

        todo_no = parse_pending_target_number(target)
        if todo_no is None:
            return None
        return self.store.find_pending_by_no(scope, group_id, user_id, todo_no)

    def _format_pending_list(self, items: list[TodoReminder]) -> str:
        """格式化未完成待办列表的群/私聊回复文本。

        Args:
            items: 当前用户当前范围内的未完成待办列表。

        Returns:
            可直接发送给用户的文本。
        """

        return self._format_todo_list("待办列表", items)

    def _format_todo_list(self, title: str, items: list[TodoReminder]) -> str:
        """格式化任意状态的待办列表。

        Args:
            title: 列表标题。
            items: 待展示的待办列表。

        Returns:
            可直接发送给用户的列表文本。
        """

        if not items:
            return "当前没有未完成待办。" if title == "待办列表" else f"当前没有{title}。"
        rows = [f"{title}："]
        for item in items:
            row = (
                f"{self._format_inline(item)}\n"
                f"   提醒时间：{self._format_time(item.remind_at)}\n"
                f"   截止时间：{self._format_time(item.due_at)}"
            )
            if item.status == STATUS_DONE:
                row += "\n   状态：已完成"
            if item.content:
                row += f"\n   内容：{_truncate(item.content, 80)}"
            rows.append(row)
        return "\n".join(rows)

    def _format_created_todos(self, items: list[TodoReminder]) -> str:
        """格式化创建待办成功后的回复文本。

        Args:
            items: 本次创建成功的待办列表。

        Returns:
            可直接发送给用户的创建结果文本。
        """

        if len(items) == 1:
            item = items[0]
            result_title = "已设置待办提醒" if item.remind_at else "已添加待办"
            return (
                f"{result_title}：\n"
                f"{self._format_inline(item)}\n"
                f"提醒时间：{self._format_time(item.remind_at)}"
            )

        rows = [f"已添加 {len(items)} 条待办："]
        for item in items:
            rows.append(
                f"{self._format_inline(item)}\n"
                f"   提醒时间：{self._format_time(item.remind_at)}"
            )
        return "\n\n".join(rows)

    def _format_inline(self, item: TodoReminder) -> str:
        """格式化待办的单行标题。

        Args:
            item: 待办记录。

        Returns:
            形如 `[1] 标题` 的文本，其中序号在当前用户和来源范围内递增。
        """

        return f"[{item.todo_no}] {_truncate(item.title, 80)}"

    def _format_time(self, timestamp: int | None) -> str:
        """把 Unix 时间戳格式化为当前配置时区下的显示文本。

        Args:
            timestamp: Unix 秒级时间戳；未设置提醒时间时传入 None。

        Returns:
            `YYYY-MM-DD HH:MM` 格式的本地时间文本，或 `未设置`。
        """

        if timestamp is None:
            return "未设置"
        return datetime.fromtimestamp(timestamp, self._timezone()).strftime("%Y-%m-%d %H:%M")

    def _timezone(self) -> ZoneInfo:
        """获取插件配置的时区对象。

        Returns:
            可用的 ZoneInfo；配置错误时回退到 Asia/Shanghai。
        """

        name = str(self.get_config("timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return ZoneInfo("Asia/Shanghai")

    @staticmethod
    def _now() -> int:
        """获取当前 Unix 秒级时间戳。

        Returns:
            当前 Unix 秒级时间戳。
        """

        return int(time.time())


def _truncate(text: str, limit: int) -> str:
    """按字符数截断长文本，避免消息列表过长。

    Args:
        text: 原始文本。
        limit: 最大字符数。

    Returns:
        不超过指定长度的文本。
    """

    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


def classify_todo_route(text: str) -> str:
    """判断一条普通消息是否应进入 Todo 路由。

    Args:
        text: 用户原始消息文本。

    Returns:
        `pending` 和 `completed` 表示确定性查询路径；`tool_loop` 表示写操作
        交给 LLM 选择工具；`none` 表示不是 Todo 请求。
    """

    normalized = " ".join((text or "").strip().split())
    if not normalized:
        return TODO_ROUTE_NONE
    if normalized.startswith("#"):
        return TODO_ROUTE_NONE

    lowered = normalized.lower()
    if lowered in _PENDING_QUERY_TEXTS:
        return TODO_ROUTE_PENDING
    if lowered in _COMPLETED_QUERY_TEXTS:
        return TODO_ROUTE_COMPLETED
    if lowered.startswith("/todo "):
        return TODO_ROUTE_TOOL_LOOP
    if not any(keyword in normalized for keyword in _TODO_WRITE_KEYWORDS):
        return TODO_ROUTE_NONE
    if normalized.startswith(_TODO_CREATE_PREFIXES):
        return TODO_ROUTE_TOOL_LOOP
    if "待办" in normalized or "todo" in lowered:
        return TODO_ROUTE_TOOL_LOOP
    if any(word in normalized for word in _TODO_REFERENCE_WORDS):
        return TODO_ROUTE_TOOL_LOOP
    if _contains_number_reference(normalized):
        return TODO_ROUTE_TOOL_LOOP
    return TODO_ROUTE_NONE


def _contains_number_reference(text: str) -> bool:
    """判断文本里是否包含可见编号引用。

    Args:
        text: 用户消息文本。

    Returns:
        包含阿拉伯数字或常见中文数字字符时返回 True。
    """

    return any(char.isdigit() for char in text) or any(char in _CHINESE_NUMBER_CHARS for char in text)


def _event_text(event: GroupMessageEvent | PrivateMessageEvent) -> str:
    """从 NcatBot 消息事件中提取纯文本。

    Args:
        event: 群聊或私聊消息事件。

    Returns:
        优先返回消息段中的文本；解析失败时回退到 raw_message。
    """

    message = getattr(event, "message", None)
    if message is not None:
        try:
            parts = [str(segment.text) for segment in message.filter_text()]
            return "".join(parts).strip()
        except Exception:
            pass
    raw_message = getattr(event, "raw_message", None)
    if raw_message is not None:
        return str(raw_message).strip()
    return ""


def _event_user_id(event: GroupMessageEvent | PrivateMessageEvent) -> str:
    """兼容从事件顶层或 sender 对象读取发送者 QQ 号。"""

    sender = getattr(event, "sender", None)
    user_id = getattr(sender, "user_id", None) if sender is not None else None
    if user_id is None:
        user_id = getattr(event, "user_id", "")
    return str(user_id)


def _positive_int(value: Any, default: int) -> int:
    """把配置值转换为正整数。

    Args:
        value: 待转换的配置值。
        default: 转换失败或不是正数时使用的默认值。

    Returns:
        正整数配置值。
    """

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
