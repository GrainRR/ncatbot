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

from .llm_parser import TodoLlmParser, TodoParseError
from .todo_store import (
    MODE_CATGIRL,
    MODE_CONCISE,
    TODO_DB_FILENAME,
    TodoReminder,
    TodoReminderDraft,
    TodoStore,
    parse_pending_target_number,
)


SCOPE_GROUP = "group"
SCOPE_PRIVATE = "private"


class TodoReminderPlugin(NcatBotPlugin):
    """基于 LLM 解析自然语言，并在指定时间发送待办提醒。"""

    name = "todo_reminder"
    version = "0.1.0"
    author = "Li"
    description = "LLM 待办提醒插件"

    store: TodoStore
    parser: TodoLlmParser
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
                "reminder_check_interval": "60s",
                "max_pending_todos_per_scope": 100,
                "max_due_reminders_per_check": 20,
                "reject_past_reminder": True,
            }
        )
        self.store = TodoStore(self.workspace / TODO_DB_FILENAME)
        self.store.init()
        self.parser = TodoLlmParser(self.config)
        self._checking_due = False
        self.add_scheduled_task(
            "check_due_todos",
            self.get_config("reminder_check_interval", "60s"),
        )
        self.logger.info("待办提醒数据库已就绪: %s", self.store.db_path)

    @registrar.qq.on_group_command("#待办")
    async def add_group_todo(self, event: GroupMessageEvent, content: str = ""):
        """在群聊中创建个人待办提醒。

        Args:
            event: 触发命令的群消息事件。
            content: 用户在 `#待办` 后输入的自然语言内容。
        """

        await self._add_todo(event, *self._group_context(event), content)

    @registrar.qq.on_private_command("#待办")
    async def add_private_todo(self, event: PrivateMessageEvent, content: str = ""):
        """在私聊中创建待办提醒。

        Args:
            event: 触发命令的私聊消息事件。
            content: 用户在 `#待办` 后输入的自然语言内容。
        """

        await self._add_todo(event, *self._private_context(event), content)

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
        await event.reply(f"已切换为{mode_name}模式，之后创建的待办会使用{mode_name}提醒文案")

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

        content = content.strip()
        if not content:
            self.logger.info(
                "待办设置失败: scope=%s group_id=%s user_id=%s reason=empty_content",
                scope,
                group_id,
                user_id,
            )
            await event.reply("请使用：#待办 明天 20:00 提醒我交作业")
            return

        max_pending = _positive_int(self.get_config("max_pending_todos_per_scope"), 100)
        if self.store.count_pending(scope, group_id, user_id) >= max_pending:
            self.logger.info(
                "待办设置失败: scope=%s group_id=%s user_id=%s reason=max_pending limit=%s raw=%s",
                scope,
                group_id,
                user_id,
                max_pending,
                _truncate(content, 100),
            )
            await event.reply(f"未完成待办已经达到上限 {max_pending} 条，请先完成或删除一些待办")
            return

        try:
            # LLM 只负责把自然语言转成结构化草稿，真正写库仍由插件端校验后完成。
            reminder_mode = self.store.get_mode(scope, group_id, user_id)
            parsed_items = await self.parser.parse(content, reminder_mode)
        except TodoParseError as exc:
            self.logger.info(
                "待办设置失败: scope=%s group_id=%s user_id=%s reason=parse_error detail=%s raw=%s",
                scope,
                group_id,
                user_id,
                exc,
                _truncate(content, 100),
            )
            await event.reply(str(exc))
            return
        except Exception as exc:
            self.logger.exception(
                "待办设置失败: scope=%s group_id=%s user_id=%s reason=unexpected_parse_error raw=%s error=%s",
                scope,
                group_id,
                user_id,
                _truncate(content, 100),
                exc,
            )
            await event.reply("解析待办失败，待办没有写入")
            return

        pending_count = self.store.count_pending(scope, group_id, user_id)
        if pending_count + len(parsed_items) > max_pending:
            self.logger.info(
                "待办设置失败: scope=%s group_id=%s user_id=%s reason=max_pending_after_parse limit=%s current=%s parsed=%s raw=%s",
                scope,
                group_id,
                user_id,
                max_pending,
                pending_count,
                len(parsed_items),
                _truncate(content, 100),
            )
            await event.reply(
                f"这次会创建 {len(parsed_items)} 条待办，未完成待办将超过上限 {max_pending} 条，"
                "请先完成或删除一些待办"
            )
            return

        drafts = [
            TodoReminderDraft(
                title=parsed.title,
                content=None,
                raw_text=content,
                remind_at=int(parsed.remind_at.timestamp()) if parsed.remind_at else None,
                due_at=int(parsed.due_at.timestamp()) if parsed.due_at else None,
                reminder_text=parsed.reminder_text,
                llm_json=parsed.raw_json,
            )
            for parsed in parsed_items
        ]
        try:
            todos = self.store.create_many(
                scope=scope,
                group_id=group_id,
                user_id=user_id,
                drafts=drafts,
                now=self._now(),
            )
        except Exception as exc:
            self.logger.exception(
                "待办设置失败: scope=%s group_id=%s user_id=%s reason=store_error raw=%s error=%s",
                scope,
                group_id,
                user_id,
                _truncate(content, 100),
                exc,
            )
            await event.reply("保存待办失败，待办没有写入")
            return

        for todo in todos:
            self.logger.info(
                "待办设置成功: scope=%s group_id=%s user_id=%s id=%s todo_no=%s remind_at=%s title=%s",
                scope,
                group_id,
                user_id,
                todo.id,
                todo.todo_no,
                todo.remind_at,
                _truncate(todo.title, 100),
            )
        await event.reply(self._format_created_todos(todos))

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

        item = self._resolve_target(scope, group_id, user_id, target)
        if item is None:
            await event.reply("请先发送 #待办列表 查看待办序号，再使用 #完成待办 1")
            return
        completed = self.store.complete(item.id)
        if completed is None:
            await event.reply("这条待办已经不是未完成状态")
            return
        await event.reply(f"已完成待办：{self._format_inline(completed)}")

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

        item = self._resolve_target(scope, group_id, user_id, target)
        if item is None:
            await event.reply("请先发送 #待办列表 查看待办序号，再使用 #删除待办 1")
            return
        deleted = self.store.cancel(item.id)
        if deleted is None:
            await event.reply("这条待办已经不是未完成状态")
            return
        await event.reply(f"已删除待办：{self._format_inline(deleted)}")

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

        text = item.reminder_text or f"待办提醒：{item.title}"

        if item.scope == SCOPE_GROUP and item.group_id:
            message = MessageArray().add_at(item.user_id).add_text(f" {text}")
            await self.api.qq.post_group_array_msg(item.group_id, message)
            return
        await self.api.qq.send_private_text(item.user_id, text)

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

        if not items:
            return "当前没有未完成待办。"
        rows = ["待办列表："]
        for item in items:
            row = (
                f"{self._format_inline(item)}\n"
                f"   提醒时间：{self._format_time(item.remind_at)}"
            )
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
