"""待办提醒插件入口。"""

from __future__ import annotations

import time
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ncatbot.core import registrar
from ncatbot.event.qq import GroupMessageEvent, PrivateMessageEvent
from ncatbot.plugin import NcatBotPlugin

from .llm import TodoToolLoop, TodoToolLoopResponse
from .proposals import (
    TodoProposalExecutionGate,
    TodoProposalPlanner,
    TodoProposalResolver,
    render_proposal,
)
from .todo_manage_tools import TodoToolContext
from .todo_manage_tools.presentation import format_concise_reminder
from .todo_store import (
    MODE_CATGIRL,
    MODE_CONCISE,
    STATUS_DONE,
    TODO_DB_FILENAME,
    TodoProposal,
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
    proposal_planner: TodoProposalPlanner
    proposal_resolver: TodoProposalResolver
    proposal_gate: TodoProposalExecutionGate
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
                "proposal_ttl_seconds": 300,
                "max_proposal_question_rounds": 3,
                "group_proposal_requires_mention": True,
            }
        )
        self.store = TodoStore(self.workspace / TODO_DB_FILENAME)
        self.store.init()
        self.tool_loop = TodoToolLoop(self.config, self.store)
        self.proposal_planner = TodoProposalPlanner(self.config, self.store)
        self.proposal_resolver = TodoProposalResolver(self.config)
        self.proposal_gate = TodoProposalExecutionGate(self.store)
        self._last_todo_numbers = {}
        self._checking_due = False
        self.add_scheduled_task(
            "check_due_todos",
            self.get_config("reminder_check_interval", "60s"),
        )
        self.add_scheduled_task(
            "expire_todo_proposals",
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
        scope, group_id, user_id = self._group_context(event)
        # Group messages still support the existing explicit Todo commands.
        # Only the potentially chatty proposal follow-up path requires an @,
        # unless this *same* member already owns an active proposal here.
        active = self.store.get_active_proposal(user_id, scope, group_id, self._now())
        allow_proposal = bool(active) or not bool(
            self.get_config("group_proposal_requires_mention", True)
        ) or self._is_bot_mentioned(event)
        await self._route_todo_message(
            event, scope, group_id, user_id, allow_proposal=allow_proposal, active_proposal=active
        )

    @registrar.qq.on_private_message()
    async def route_private_todo_message(self, event: PrivateMessageEvent):
        """把普通私聊消息中的 Todo 查询和写操作路由到确定性路径或 Tool Loop。

        Args:
            event: 收到的私聊消息事件。
        """

        await self._route_todo_message(
            event, *self._private_context(event), allow_proposal=True, active_proposal=None
        )

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
        allow_proposal: bool,
        active_proposal: TodoProposal | None,
    ) -> None:
        """Route one text event through exactly one Todo response path.

        Priority is a business/safety rule: explicit built-in commands and a
        new complete tool request replace a pending conversational proposal;
        only a remaining reply is sent to the proposal resolver.  A group may
        enter the proposal path only after a bot mention or for its owner's
        existing proposal, so one member's options cannot spill into chat.

        Args:
            event: Current text event; it is replied to at most once.
            scope/group_id/user_id: Trusted session binding used by all store
                transitions and by the executor context.
            allow_proposal: Private chats are true; group policy supplies the
                mention/active-proposal decision.
            active_proposal: Optional preloaded group proposal.  Private
                callers may pass None and it is loaded from SQLite here.
        """

        text = _event_text(event)
        normalized = " ".join(text.strip().split())
        if not normalized or self._is_bot_own_message(event):
            return
        # Group configuration commands were handled by the group entrypoint;
        # every other system-style command stays outside both the direct Tool
        # Loop and the LLM proposal path.  A leading ``#`` must never create
        # an unsolicited conversational proposal in a private chat either.
        if normalized.startswith("#"):
            return
        active = active_proposal or self.store.get_active_proposal(
            user_id, scope, group_id, self._now()
        )
        if normalized in _MODE_CATGIRL_TEXTS:
            await self._replace_pending_for_new_request(active, scope, group_id, user_id)
            await self._switch_mode(event, scope, group_id, user_id, MODE_CATGIRL)
            return
        if normalized in _MODE_CONCISE_TEXTS:
            await self._replace_pending_for_new_request(active, scope, group_id, user_id)
            await self._switch_mode(event, scope, group_id, user_id, MODE_CONCISE)
            return

        route = classify_todo_route(text)
        direct_tool_request = route == TODO_ROUTE_TOOL_LOOP and is_complete_todo_tool_request(text)
        if active is not None and not _is_proposal_reply_text(normalized, active):
            if route in {TODO_ROUTE_PENDING, TODO_ROUTE_COMPLETED} or direct_tool_request:
                await self._replace_pending_for_new_request(active, scope, group_id, user_id)
                active = None

        if active is not None:
            # An active proposal is always allowed through this resolver in
            # its own session, even in groups where ordinary text is ignored.
            await self._resolve_pending_proposal(
                event, active, scope, group_id, user_id, text
            )
            return
        if route == TODO_ROUTE_PENDING:
            await self._list_todos(event, scope, group_id, user_id)
            return
        if route == TODO_ROUTE_COMPLETED:
            await self._list_completed_todos(event, scope, group_id, user_id)
            return
        if route == TODO_ROUTE_TOOL_LOOP:
            # A matching keyword is not enough to authorize a direct write.
            # Requests such as “修改第二条” still lack an edit value, and
            # “提醒我关窗” normally lacks a time.  They must become a
            # persisted proposal/question instead of trusting the first LLM
            # tool call to fill in the missing intent.
            if direct_tool_request:
                await self._run_todo_tool_loop(
                    event, scope, group_id, user_id, text, fallback_to_proposal=allow_proposal
                )
            elif allow_proposal:
                await self._create_proposal(event, scope, group_id, user_id, text)
            return
        if allow_proposal:
            await self._create_proposal(event, scope, group_id, user_id, text)

    async def _run_todo_tool_loop(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
        user_text: str,
        fallback_to_proposal: bool = False,
    ) -> TodoToolLoopResponse | None:
        """Execute a recognized complete request or safely switch to planning.

        Existing complete writes keep their direct Tool Loop behaviour.  If a
        model cannot form any tool call, no database mutation has occurred;
        eligible sessions are then handed to the independent proposal planner
        instead of exposing a fixed, context-free follow-up message.

        Args:
            event: 收到的群聊或私聊消息事件。
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            user_text: 交给 Tool Loop 的规范化用户文本。
            fallback_to_proposal: Whether group mention/private policy permits
                an ambiguous request to open a persisted proposal.

        Returns:
            The direct loop response when available.  Exceptions are replied
            once and return None; they never create an executable proposal.
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
            return None
        self._remember_tool_results(scope, group_id, user_id, response.tool_results)
        if fallback_to_proposal and not response.tool_results:
            await self._create_proposal(event, scope, group_id, user_id, user_text)
            return response
        await event.reply(response.message)
        return response

    async def _create_proposal(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        group_id: str | None,
        user_id: str,
        user_text: str,
        question_rounds: int = 0,
    ) -> None:
        """Plan and persist an unexecuted candidate/question, then render it.

        The planner's calls are only proposals.  This method is the sole
        writer of ``PENDING_PROPOSAL`` rows and intentionally runs after all
        direct-command priorities.  Replacing a pending row is atomic in the
        store, so a stale reply cannot execute the prior intent.
        """

        max_rounds = _positive_int(self.get_config("max_proposal_question_rounds"), 3)
        if question_rounds > max_rounds:
            await event.reply("需要的信息仍不完整，本次待办操作已结束。请重新完整说明。")
            return
        context = self._tool_context(scope, group_id, user_id, user_text)
        plan = await self.proposal_planner.plan(user_text, context)
        proposal = self.store.create_proposal(
            user_id,
            scope,
            group_id,
            user_text,
            plan.question_text,
            plan.candidates,
            self._now(),
            _positive_int(self.get_config("proposal_ttl_seconds"), 300),
            question_rounds,
        )
        rendered = render_proposal(proposal)
        if scope == SCOPE_GROUP:
            rendered += "\n请回到发起待办的群聊回复编号、确认或取消。"
        await self._reply_proposal_message(event, scope, user_id, rendered)

    async def _resolve_pending_proposal(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        proposal: TodoProposal,
        scope: str,
        group_id: str | None,
        user_id: str,
        reply_text: str,
    ) -> None:
        """Resolve one reply, without allowing reply parsing to execute tools.

        State transitions:
            accept -> atomic claim -> gate -> EXECUTED;
            reject -> REJECTED;
            replace/clarify -> old row REPLACED and a new pending proposal.
        Every branch replies once.  The resolver itself only returns an enum;
        only the accepted branch reaches the execution gate.
        """

        context = self._tool_context(scope, group_id, user_id, proposal.source_text)
        resolution = await self.proposal_resolver.resolve(reply_text, proposal, context)
        if resolution.action == "accept" and resolution.option_id is not None:
            await self._execute_accepted_proposal(
                event, proposal, resolution.option_id, scope, group_id, user_id
            )
            return
        if resolution.action == "reject":
            outcome = self.store.reject_proposal(
                proposal.token, user_id, scope, group_id, self._now()
            )
            message = "已取消本次待确认操作。" if outcome == "rejected" else "该待确认操作已失效。"
            await self._reply_proposal_message(event, scope, user_id, message)
            return
        if resolution.action == "replace" and resolution.new_text:
            await self._create_proposal(event, scope, group_id, user_id, resolution.new_text)
            return

        # A clarification is not a hidden acceptance.  It becomes a new
        # proposal whose source includes the supplement, bounded by rounds.
        next_round = proposal.question_rounds + 1
        if next_round > _positive_int(self.get_config("max_proposal_question_rounds"), 3):
            self.store.reject_proposal(proposal.token, user_id, scope, group_id, self._now())
            await self._reply_proposal_message(
                event, scope, user_id, "需要的信息仍不完整，本次待办操作已结束。请重新完整说明。"
            )
            return
        combined = f"原请求：{proposal.source_text}\n补充信息：{reply_text.strip()}"
        await self._create_proposal(event, scope, group_id, user_id, combined, next_round)

    async def _execute_accepted_proposal(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        proposal: TodoProposal,
        option_id: int,
        scope: str,
        group_id: str | None,
        user_id: str,
    ) -> None:
        """Claim, revalidate and execute exactly one previously stored option.

        The claim is a persistent compare-and-set keyed by proposal/session/
        event.  The gate then repeats expiry, Schema and stable target checks.
        A duplicate response never crosses the executor boundary; permanent
        deletion still receives the existing token-confirmation behaviour.
        """

        outcome, accepted, candidate = self.store.claim_proposal_for_execution(
            proposal.token,
            option_id,
            user_id,
            scope,
            group_id,
            _event_identity(event),
            self._now(),
        )
        if outcome == "duplicate":
            await self._reply_proposal_message(
                event,
                scope,
                user_id,
                (accepted.execution_result if accepted else None) or "该确认已处理，请不要重复发送。",
            )
            return
        if outcome != "accepted" or accepted is None or candidate is None:
            messages = {
                "expired": "该待确认操作已过期，未执行任何待办操作。",
                "mismatch": "该确认不属于当前会话，未执行任何待办操作。",
                "invalid_option": "候选项无效，请重新说明。",
                "closed": "该待确认操作已关闭，未执行任何待办操作。",
                "missing": "找不到待确认操作，请重新说明。",
            }
            await self._reply_proposal_message(event, scope, user_id, messages.get(outcome, "待确认操作无法执行。"))
            return
        context = self._tool_context(scope, group_id, user_id, accepted.source_text)
        try:
            result = self.proposal_gate.execute(accepted, candidate, context)
        except Exception as exc:
            self.logger.exception("提议执行闸门异常 token=%s: %s", accepted.token, exc)
            result_message = "待确认操作执行失败，数据库未变更。"
        else:
            result_message = result.message
            self._remember_tool_results(scope, group_id, user_id, [result])
        # Terminalize before replying so a redelivered event cannot execute a
        # write while the chat transport is retrying this response.
        self.store.complete_proposal_execution(accepted.token, result_message, self._now())
        await self._reply_proposal_message(event, scope, user_id, result_message)

    async def _replace_pending_for_new_request(
        self,
        proposal: TodoProposal | None,
        scope: str,
        group_id: str | None,
        user_id: str,
    ) -> None:
        """Close prior pending intent before a preset/direct request proceeds."""

        if proposal is not None:
            self.store.replace_proposal(proposal.token, user_id, scope, group_id, self._now())

    async def _reply_proposal_message(
        self,
        event: GroupMessageEvent | PrivateMessageEvent,
        scope: str,
        user_id: str,
        text: str,
    ) -> None:
        """Deliver proposal-only content without leaking it to a group.

        Candidate choices include personal Todo titles and stable history IDs.
        In a group they are therefore sent only to the proposing member via
        private message.  A failed private delivery gets one generic group
        response with no option, target or model-derived content.
        """

        if scope != SCOPE_GROUP:
            await event.reply(text)
            return
        try:
            await self.api.qq.send_private_text(user_id, text)
        except Exception as exc:
            self.logger.warning("无法私聊发送群待办候选 user_id=%s: %s", user_id, exc)
            await event.reply("无法私聊发送待办确认，请先允许机器人私聊后重试。")

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

    async def expire_todo_proposals(self) -> None:
        """Periodically close expired proposal rows without relying on memory.

        Lookups and the execution claim independently check expiry, so this
        task is only prompt cleanup.  It improves conversational behaviour
        after long idle periods and makes state transitions observable.
        """

        expired = self.store.expire_proposals(self._now())
        if expired:
            self.logger.info("已关闭 %s 条过期待确认待办操作", expired)

    async def _send_reminder(self, item: TodoReminder) -> bool:
        """发送单条到期待办提醒。

        Args:
            item: 已到期且尚未提醒的待办记录。
        """

        if self._reminder_mode(item.scope, item.group_id, item.user_id) == MODE_CONCISE:
            text = format_concise_reminder(item, self._timezone())
        else:
            text = item.reminder_text or f"待办提醒：{item.title}"

        await self.api.qq.send_private_text(item.user_id, text)
        return True

    def _is_bot_mentioned(self, event: GroupMessageEvent) -> bool:
        """Detect an explicit @ to this bot without treating @all as consent.

        NcatBot adapters expose message segments differently across versions,
        so this method checks typed segments first and safely falls back to
        CQ/raw text.  If the bot identity is unavailable it returns false:
        suppressing an optional group proposal is safer than replying to every
        group message.
        """

        bot_id = _event_bot_id(event, getattr(self, "api", None))
        if not bot_id:
            return False
        message = getattr(event, "message", None)
        try:
            segments = list(message) if message is not None and not isinstance(message, str) else []
        except TypeError:
            segments = []
        for segment in segments:
            kind = str(getattr(segment, "type", "") or getattr(segment, "segment_type", "")).lower()
            if kind not in {"at", "mention"} and "at" not in type(segment).__name__.lower():
                continue
            data = getattr(segment, "data", None)
            values = [
                getattr(segment, name, None)
                for name in ("qq", "user_id", "target", "target_id")
            ]
            if isinstance(data, dict):
                values.extend(data.get(name) for name in ("qq", "user_id", "target", "target_id"))
            if any(str(value) == bot_id for value in values if value is not None):
                return True
        raw = str(getattr(event, "raw_message", "") or "")
        return f"[CQ:at,qq={bot_id}" in raw or f"@{bot_id}" in raw

    def _is_bot_own_message(self, event: GroupMessageEvent | PrivateMessageEvent) -> bool:
        """Ignore echoed outbound messages when the adapter exposes self ID."""

        bot_id = _event_bot_id(event, getattr(self, "api", None))
        return bool(bot_id and _event_user_id(event) == bot_id)

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


def is_complete_todo_tool_request(text: str) -> bool:
    """Return whether a recognised write request is safe for the direct loop.

    This is deliberately a *conservative* routing test, not a second natural
    language parser.  Direct execution remains available for the established
    unambiguous forms, while anything that could require a value, time or
    target guess is delegated to the confirm-before-execute proposal flow.
    The Tool Loop and executor still perform their normal validation after
    this check; this function merely prevents an LLM from completing missing
    user intent by invention.

    Args:
        text: The original text from a private message or an enabled group.

    Returns:
        ``True`` only for an explicit command or a recognisable complete
        operation.  ``False`` means the caller may create a proposal if the
        session's group policy permits it.
    """

    normalized = " ".join((text or "").strip().split())
    lowered = normalized.lower()
    if not normalized:
        return False
    # An explicit /todo command is an intentional request for the pre-existing
    # Tool Loop interface, including its own clarifications and safeguards.
    if lowered.startswith("/todo "):
        return True

    # A create prefix plus a non-empty title is complete because reminders are
    # intentionally optional in this plugin.
    if normalized.startswith(_TODO_CREATE_PREFIXES):
        remainder = normalized
        for prefix in _TODO_CREATE_PREFIXES:
            if normalized.startswith(prefix):
                remainder = normalized[len(prefix) :].strip(" ：:")
                break
        return bool(remainder)

    has_number = _contains_number_reference(normalized) or any(
        word in normalized for word in _TODO_REFERENCE_WORDS
    )
    has_history_id = bool(re.search(r"\bH-[A-Za-z0-9_-]+\b", normalized))
    has_minutes = bool(re.search(r"\d+\s*(?:分钟|分|小时|钟头)", normalized))
    has_time = has_minutes or bool(
        re.search(
            r"(?:今天|明天|后天|下周|今晚|上午|下午|晚上|\d{1,2}\s*[:：]\s*\d{1,2}|\d{1,2}\s*点)",
            normalized,
        )
    )

    if any(word in normalized for word in ("完成", "取消")):
        return has_number
    if "合并" in normalized:
        return len(re.findall(r"\d+", normalized)) >= 2
    if "恢复" in normalized or "永久删除" in normalized:
        return has_history_id
    if "删除" in normalized:
        # The executor's separate permanent-delete confirmation remains
        # mandatory even when this request is direct.
        return has_history_id or has_number
    if any(word in normalized for word in ("推迟", "延后", "提前", "晚点", "稍后")):
        return has_number and has_minutes
    if any(word in normalized for word in ("修改", "更改", "改成", "改为", "改提醒", "改时间", "提醒调整")):
        # A bare “修改第 2 条” has no field value.  Require a target and some
        # textual payload beyond the action/reference before entering direct
        # execution; exact field validation remains in the backend tool.
        if not has_number:
            return False
        stripped = re.sub(r"(?:第?\s*\d+\s*条?|上一条|前一条|刚才(?:那个|那条)|刚刚(?:那个|那条))", "", normalized)
        return len(stripped) >= 6 and (has_time or any(word in stripped for word in ("标题", "内容", "为", "成")))
    if "提醒我" in normalized:
        # “提醒我关窗” needs clarification; a concrete time is complete.
        return has_time
    return False


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


def _event_bot_id(event: Any, api: Any | None) -> str | None:
    """Return the adapter's bot identity when it is safely discoverable.

    The helper intentionally has no guessed default.  Group proposal policy
    must require a real mention of this bot, not merely any ``@`` segment.
    """

    candidates = [
        getattr(event, "self_id", None),
        getattr(event, "bot_id", None),
        getattr(api, "self_id", None) if api is not None else None,
        getattr(api, "bot_id", None) if api is not None else None,
    ]
    qq = getattr(api, "qq", None) if api is not None else None
    if qq is not None:
        candidates.extend((getattr(qq, "self_id", None), getattr(qq, "bot_id", None)))
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return None


def _event_identity(event: GroupMessageEvent | PrivateMessageEvent) -> str | None:
    """Extract a stable inbound event ID for proposal execution idempotency."""

    for field_name in ("message_id", "real_id", "message_seq"):
        value = getattr(event, field_name, None)
        if value is not None and str(value).strip():
            return f"{field_name}:{value}"
    return None


def _is_proposal_reply_text(text: str, proposal: TodoProposal) -> bool:
    """Recognize terse reply forms before treating them as a new direct task.

    This preserves the intentional meaning of ``1``, ``是`` and ``取消`` in
    a pending proposal.  Longer messages and explicit built-in commands keep
    route priority and replace the old proposal instead.
    """

    normalized = " ".join((text or "").strip().split()).lower()
    if normalized in {"是", "好的", "好", "确认", "确定", "可以", "行", "否", "不", "不要", "取消", "算了", "拒绝"}:
        return True
    selected = _proposal_option_number(normalized)
    if selected is not None:
        return 1 <= selected <= len(proposal.candidates) + (1 if proposal.candidates else 0)
    return normalized.startswith("不，是") or normalized.startswith("不对，是")


def _proposal_option_number(text: str) -> int | None:
    """Parse only explicit human-facing proposal option forms."""

    if text.isdigit():
        return int(text)
    for prefix in ("选", "选择", "第"):
        if text.startswith(prefix):
            suffix = text[len(prefix) :].strip()
            if suffix.endswith("个") or suffix.endswith("项"):
                suffix = suffix[:-1].strip()
            if suffix.isdigit():
                return int(suffix)
    return None


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
