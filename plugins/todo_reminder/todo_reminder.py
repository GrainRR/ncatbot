"""待办提醒插件入口。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ncatbot.core import registrar
from ncatbot.event.qq import GroupMessageEvent, PrivateMessageEvent
from ncatbot.plugin import NcatBotPlugin
from ncatbot.types import MessageArray

from .llm import TodoToolLoop
from .llm_parser import (
    TODO_PREPROCESS_CLARIFY,
    TODO_PREPROCESS_COMPLETED,
    TODO_PREPROCESS_PENDING,
    TODO_PREPROCESS_TOOL_LOOP,
    preprocess_hash_todo_content,
    preprocess_todo_command,
    render_reminder_text,
)
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


class TodoReminderPlugin(NcatBotPlugin):
    """基于 LLM 解析自然语言，并在指定时间发送待办提醒。"""

    name = "todo_reminder"
    version = "0.1.0"
    author = "Li"
    description = "LLM 待办提醒插件"

    store: TodoStore
    tool_loop: TodoToolLoop
    _last_todo_numbers: dict[tuple[str, str, str], int]
    _checking_due: bool

    async def on_load(self):
        """插件加载时初始化配置、数据库、LLM 解析器和定时扫描任务。"""

        self.init_defaults(
            {
                "llm_api_base": "",
                "llm_api_url": "",
                "llm_api_key": "",
                "llm_api_key_env": "TODO_REMINDER_LLM_API_KEY",
                "llm_model": "",
                "llm_timeout_seconds": 30,
                "timezone": "Asia/Shanghai",
                "default_reminder_mode": MODE_CATGIRL,
                "reminder_check_interval": "60s",
                "max_pending_todos_per_scope": 100,
                "max_due_reminders_per_check": 20,
                "reject_past_reminder": True,
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

    async def add_group_todo(self, event: GroupMessageEvent, content: str = ""):
        """在群聊中创建个人待办提醒。

        Args:
            event: 触发命令的群消息事件。
            content: 用户在 `#待办` 后输入的自然语言内容。
        """

        await self._add_todo(event, *self._group_context(event), content)

    async def add_private_todo(self, event: PrivateMessageEvent, content: str = ""):
        """在私聊中创建待办提醒。

        Args:
            event: 触发命令的私聊消息事件。
            content: 用户在 `#待办` 后输入的自然语言内容。
        """

        await self._add_todo(event, *self._private_context(event), content)

    @registrar.qq.on_group_message()
    async def route_group_todo_message(self, event: GroupMessageEvent):
        """把普通群消息中的 Todo 查询和写操作路由到确定性路径或 Tool Loop。"""

        await self._route_todo_message(event, *self._group_context(event))

    @registrar.qq.on_private_message()
    async def route_private_todo_message(self, event: PrivateMessageEvent):
        """把普通私聊消息中的 Todo 查询和写操作路由到确定性路径或 Tool Loop。"""

        await self._route_todo_message(event, *self._private_context(event))

    @registrar.qq.on_group_command("#待办列表")
    async def list_group_todos(self, event: GroupMessageEvent):
        """查看当前用户在当前群内创建的未完成待办。

        Args:
            event: 触发命令的群消息事件。
        """

        await self._list_todos(event, *self._group_context(event))

    @registrar.qq.on_private_command("#待办列表")
    async def list_private_todos(self, event: PrivateMessageEvent):
        """查看当前用户在私聊中创建的未完成待办。

        Args:
            event: 触发命令的私聊消息事件。
        """

        await self._list_todos(event, *self._private_context(event))

    @registrar.qq.on_group_command("#待办-猫娘模式")
    async def set_group_catgirl_mode(self, event: GroupMessageEvent):
        """把当前用户在当前群内的提醒模式切换为猫娘模式。

        Args:
            event: 触发命令的群消息事件。
        """

        await self._switch_mode(event, *self._group_context(event), MODE_CATGIRL)

    @registrar.qq.on_private_command("#待办-猫娘模式")
    async def set_private_catgirl_mode(self, event: PrivateMessageEvent):
        """把当前用户在私聊中的提醒模式切换为猫娘模式。

        Args:
            event: 触发命令的私聊消息事件。
        """

        await self._switch_mode(event, *self._private_context(event), MODE_CATGIRL)

    @registrar.qq.on_group_command("#待办-简洁模式")
    async def set_group_concise_mode(self, event: GroupMessageEvent):
        """把当前用户在当前群内的提醒模式切换为简洁模式。

        Args:
            event: 触发命令的群消息事件。
        """

        await self._switch_mode(event, *self._group_context(event), MODE_CONCISE)

    @registrar.qq.on_private_command("#待办-简洁模式")
    async def set_private_concise_mode(self, event: PrivateMessageEvent):
        """把当前用户在私聊中的提醒模式切换为简洁模式。

        Args:
            event: 触发命令的私聊消息事件。
        """

        await self._switch_mode(event, *self._private_context(event), MODE_CONCISE)

    @registrar.qq.on_group_command("#完成待办")
    async def complete_group_todo(self, event: GroupMessageEvent, target: str = ""):
        """完成当前用户在当前群内的一条待办。

        Args:
            event: 触发命令的群消息事件。
            target: 当前群和当前用户范围内的待办序号。
        """

        await self._complete_todo(event, *self._group_context(event), target)

    @registrar.qq.on_private_command("#完成待办")
    async def complete_private_todo(self, event: PrivateMessageEvent, target: str = ""):
        """完成当前用户在私聊中的一条待办。

        Args:
            event: 触发命令的私聊消息事件。
            target: 当前私聊用户范围内的待办序号。
        """

        await self._complete_todo(event, *self._private_context(event), target)

    @registrar.qq.on_group_command("#删除待办")
    async def delete_group_todo(self, event: GroupMessageEvent, target: str = ""):
        """软删除当前用户在当前群内的一条待办。

        Args:
            event: 触发命令的群消息事件。
            target: 当前群和当前用户范围内的待办序号。
        """

        await self._delete_todo(event, *self._group_context(event), target)

    @registrar.qq.on_private_command("#删除待办")
    async def delete_private_todo(self, event: PrivateMessageEvent, target: str = ""):
        """软删除当前用户在私聊中的一条待办。

        Args:
            event: 触发命令的私聊消息事件。
            target: 当前私聊用户范围内的待办序号。
        """

        await self._delete_todo(event, *self._private_context(event), target)

    def _group_context(self, event: GroupMessageEvent) -> tuple[str, str | None, str]:
        """生成群聊命令使用的待办范围参数。

        Args:
            event: 触发命令的群消息事件。

        Returns:
            依次返回范围类型、群号和用户 QQ 号。
        """

        return SCOPE_GROUP, str(event.group_id), str(event.user_id)

    def _private_context(self, event: PrivateMessageEvent) -> tuple[str, str | None, str]:
        """生成私聊命令使用的待办范围参数。

        Args:
            event: 触发命令的私聊消息事件。

        Returns:
            依次返回范围类型、空群号和用户 QQ 号。
        """

        return SCOPE_PRIVATE, None, str(event.user_id)

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
        """回复当前范围内的已完成待办列表。"""

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

    async def _add_todo(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
        content: str,
    ) -> None:
        """创建待办提醒的通用实现。

        Args:
            event: 触发命令的消息事件，用于回复用户。
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            content: 用户输入的自然语言待办内容。
        """

        preprocessed = preprocess_hash_todo_content(content)
        if preprocessed.route == TODO_PREPROCESS_PENDING:
            await self._list_todos(event, scope, group_id, user_id)
            return
        if preprocessed.route == TODO_PREPROCESS_COMPLETED:
            await self._list_completed_todos(event, scope, group_id, user_id)
            return
        if preprocessed.route == TODO_PREPROCESS_CLARIFY:
            await event.reply(preprocessed.clarify_message)
            return
        if preprocessed.route != TODO_PREPROCESS_TOOL_LOOP:
            return

        await self._run_todo_tool_loop(
            event,
            scope,
            group_id,
            user_id,
            preprocessed.normalized_text,
        )

    async def _complete_todo(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
        target: str,
    ) -> None:
        """完成待办的通用实现。

        Args:
            event: 触发命令的消息事件，用于回复用户。
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            target: 用户输入的范围内待办序号。
        """

        await self._run_todo_tool_loop(event, scope, group_id, user_id, f"完成待办 {target}")

    async def _delete_todo(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
        target: str,
    ) -> None:
        """软删除待办的通用实现。

        Args:
            event: 触发命令的消息事件，用于回复用户。
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            target: 用户输入的范围内待办序号。
        """

        await self._run_todo_tool_loop(event, scope, group_id, user_id, f"取消待办 {target}")

    async def _route_todo_message(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
    ) -> None:
        """普通消息的 Todo 程序路由。"""

        text = _event_text(event)
        preprocessed = preprocess_todo_command(text)
        if preprocessed.is_hash_todo:
            if preprocessed.route == TODO_PREPROCESS_PENDING:
                await self._list_todos(event, scope, group_id, user_id)
                return
            if preprocessed.route == TODO_PREPROCESS_COMPLETED:
                await self._list_completed_todos(event, scope, group_id, user_id)
                return
            if preprocessed.route == TODO_PREPROCESS_CLARIFY:
                await event.reply(preprocessed.clarify_message)
                return
            if preprocessed.route == TODO_PREPROCESS_TOOL_LOOP:
                await self._run_todo_tool_loop(
                    event,
                    scope,
                    group_id,
                    user_id,
                    preprocessed.normalized_text,
                )
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
        """执行 Todo Tool Loop 并基于真实工具结果回复。"""

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
        """构造传给后端工具的可信上下文。"""

        return TodoToolContext(
            scope=scope,
            group_id=group_id,
            user_id=user_id,
            now=self._now(),
            timezone=self._timezone(),
            max_pending=_positive_int(self.get_config("max_pending_todos_per_scope"), 100),
            reject_past_reminder=bool(self.get_config("reject_past_reminder", True)),
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
        """记录最后一次工具操作涉及的用户可见编号，支持“刚才那个”等引用。"""

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
    def _context_key(scope: str, group_id: str | None, user_id: str) -> tuple[str, str, str]:
        return scope, group_id or "", user_id

    async def check_due_todos(self) -> None:
        """定时扫描到期待办，并按来源发送群聊或私聊提醒。"""

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
                    self.store.mark_reminded(item.id, self._now())
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

    async def _send_reminder(self, item: TodoReminder) -> None:
        """发送单条到期待办提醒。

        Args:
            item: 已到期且尚未提醒的待办记录。
        """

        mode = self._reminder_mode(item.scope, item.group_id, item.user_id)
        text = render_reminder_text(
            item.title,
            item.content or item.reminder_text or None,
            mode,
        )

        if item.scope == SCOPE_GROUP and item.group_id:
            message = MessageArray().add_at(item.user_id).add_text(f" {text}")
            await self.api.qq.post_group_array_msg(item.group_id, message)
            return
        await self.api.qq.send_private_text(item.user_id, text)

    def _reminder_mode(self, scope: str, group_id: str | None, user_id: str) -> str:
        """获取当前提醒展示风格。"""

        mode = self.store.get_mode(scope, group_id, user_id)
        if mode not in {MODE_CONCISE, MODE_CATGIRL}:
            configured = str(self.get_config("default_reminder_mode", MODE_CATGIRL) or MODE_CATGIRL)
            return configured if configured in {MODE_CONCISE, MODE_CATGIRL} else MODE_CATGIRL
        return mode

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
        """格式化任意状态的待办列表。"""

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
        """获取当前 Unix 秒级时间戳。"""

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
    if normalized.startswith(("新增", "添加", "创建", "新建", "加个", "加一条")):
        return TODO_ROUTE_TOOL_LOOP
    if "待办" in normalized or "todo" in lowered:
        return TODO_ROUTE_TOOL_LOOP
    if any(word in normalized for word in _TODO_REFERENCE_WORDS):
        return TODO_ROUTE_TOOL_LOOP
    if _contains_number_reference(normalized):
        return TODO_ROUTE_TOOL_LOOP
    return TODO_ROUTE_NONE


def _contains_number_reference(text: str) -> bool:
    return any(char.isdigit() for char in text) or any(char in _CHINESE_NUMBER_CHARS for char in text)


def _event_text(event: GroupMessageEvent | PrivateMessageEvent) -> str:
    """从 NcatBot 消息事件中提取纯文本。"""

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
