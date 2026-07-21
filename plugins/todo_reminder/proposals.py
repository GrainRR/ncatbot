"""Persistent, confirm-before-execute proposal flow for ambiguous Todo input.

The module deliberately separates three activities that must never collapse
into one another:

* :class:`TodoProposalPlanner` asks an LLM for *candidate* tool calls.
* :class:`TodoProposalResolver` classifies a later human reply.
* :class:`TodoProposalExecutionGate` revalidates and then calls the real
  executor only after the persistent state machine accepted one candidate.

Keeping this boundary here makes it difficult for a model response, a forged
message, or process-local state to become a database write by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .llm.openai_chat import ChatCompletionChoice, OpenAICompatibleChatClient
from .todo_manage_tools import TodoToolContext, TodoToolExecutor, ToolResult
from .todo_manage_tools.contracts import ToolExecutionStop, ToolValidationError
from .todo_manage_tools.registry import TOOL_SPECS_BY_NAME
from .todo_manage_tools.targets import REFERENCE_WORDS
from .todo_manage_tools.validation import validate_json_schema
from .todo_store import (
    PROPOSAL_ACCEPTED,
    PROPOSAL_PENDING,
    STATUS_DELETED,
    STATUS_DONE,
    STATUS_OPEN,
    TodoProposal,
    TodoProposalCandidate,
    TodoReminder,
    TodoStore,
)


MAX_PROPOSAL_CANDIDATES = 3


_PLANNER_SYSTEM_PROMPT = """你是待办操作的候选规划器，不是执行器。
只能在信息足够形成一个安全、可执行的候选时调用 Todo 工具；这些调用仅用于提出候选，绝不表示操作已完成。
把一次工具调用视为一个可供用户选择的候选。通常只给一个；确有多个合理解释时最多三个，不能把连续多步操作拆成多个候选。
如果时间、待办编号或目标含义不足，绝对不要猜测参数、不要调用工具，只用普通中文提出与用户原意直接相关的问题。
目标只能使用给定列表里用户可见的待办编号或历史 ID，不能编造 ID、不能提及内部数据库 ID。
永久删除的工具调用若没有确认令牌只会发起独立二次确认，不能声称已经删除。
"""

_RESOLVER_SYSTEM_PROMPT = """你是待办候选回复分类器，绝不能执行待办操作。
根据候选、用户原请求和用户最新回复，只能调用 resolve_todo_proposal_reply：
accept 表示明确接受某个候选；reject 表示拒绝；replace 表示用户提出了新的完整要求；clarify 表示仍不明确或只是补充信息。
如果用户回复是明确的新待办请求，例如“查看待办”或“十分钟后提醒我关窗户”，选择 replace，不能把它当作接受。
不要在普通文本中声称任何操作已完成。
"""


@dataclass(frozen=True)
class TodoProposalPlan:
    """Planner output before it is persisted.

    ``candidates`` have passed tool-schema and stable-target validation, but
    no executor has run and no SQLite Todo record has been changed.
    """

    question_text: str
    candidates: tuple[TodoProposalCandidate, ...]


@dataclass(frozen=True)
class TodoProposalResolution:
    """One of the only allowed reply-parser outcomes."""

    action: str
    option_id: int | None = None
    new_text: str | None = None


class TodoProposalPlanner:
    """Generate validated candidate tool calls without performing mutations.

    Inputs:
        User text and a trusted :class:`TodoToolContext` constructed by the
        router.  The context pins user/session/timezone and cannot be supplied
        by an LLM.
    Outputs:
        Up to three persisted-safe candidates or an LLM-authored clarifying
        question.
    Side effects:
        Network I/O to the configured LLM only.  It never invokes
        ``TodoToolExecutor.execute`` and never writes Todo data.
    Security/concurrency:
        Every returned call is checked again against the backend's one true
        Schema registry and targets are converted to stable history snapshots.
        A later execution must recheck those snapshots because planning and
        acceptance can be separated by messages or a process restart.
    """

    def __init__(self, config: dict[str, Any], store: TodoStore, client: Any | None = None) -> None:
        self.config = config
        self.store = store
        self.client = client or OpenAICompatibleChatClient(config)

    async def plan(self, user_text: str, context: TodoToolContext) -> TodoProposalPlan:
        """Ask the LLM for alternatives and return only safely usable ones.

        Invalid tool names, Schema violations, inaccessible targets, and
        malformed model output are discarded.  If nothing remains, ordinary
        LLM text is used strictly as a question; an unavailable/invalid model
        gets a non-committal fallback question rather than a guessed action.
        """

        tool_definitions = TodoToolExecutor(self.store, context).tool_definitions
        try:
            choice = await self.client.complete_with_tools(
                self._build_messages(user_text, context), tool_definitions
            )
        except Exception:
            return TodoProposalPlan("我还需要你补充待办的具体内容、编号或时间。", ())
        if not isinstance(choice, ChatCompletionChoice):
            choice = ChatCompletionChoice(
                content=str(getattr(choice, "content", "") or ""),
                tool_calls=list(getattr(choice, "tool_calls", []) or []),
            )

        candidates: list[TodoProposalCandidate] = []
        for call in choice.tool_calls:
            if len(candidates) >= MAX_PROPOSAL_CANDIDATES:
                break
            candidate = self._validated_candidate(call.name, call.arguments, context)
            if candidate is not None and not any(
                existing.tool_name == candidate.tool_name
                and existing.arguments == candidate.arguments
                for existing in candidates
            ):
                candidates.append(candidate)

        numbered = tuple(
            TodoProposalCandidate(
                option_id=index,
                tool_name=candidate.tool_name,
                arguments=candidate.arguments,
                target_snapshots=candidate.target_snapshots,
            )
            for index, candidate in enumerate(candidates, start=1)
        )
        if numbered:
            # The visual proposal is backend-rendered from these arguments;
            # model prose may not manufacture user-visible actions.
            return TodoProposalPlan("", numbered)
        question = choice.content.strip()
        if not question or _looks_like_unsafe_success_claim(question):
            question = "我还需要你补充待办的具体内容、编号或时间。"
        return TodoProposalPlan(question, ())

    def _build_messages(self, user_text: str, context: TodoToolContext) -> list[dict[str, str]]:
        """Build a least-privilege planning prompt with visible Todo summaries."""

        pending = self.store.list_by_status(
            context.scope, context.group_id, context.user_id, STATUS_OPEN, 20
        )
        completed = self.store.list_by_status(
            context.scope, context.group_id, context.user_id, STATUS_DONE, 20
        )
        canceled = self.store.list_by_status(
            context.scope, context.group_id, context.user_id, STATUS_DELETED, 20
        )
        current_time = datetime.fromtimestamp(context.now, context.timezone)
        user_prompt = (
            "当前用户可见的未完成待办：\n"
            f"{_format_visible_todos(pending)}\n\n"
            "当前用户可见的已完成待办：\n"
            f"{_format_visible_todos(completed)}\n\n"
            "当前用户可见的已取消待办：\n"
            f"{_format_visible_todos(canceled)}\n\n"
            f"当前时间：{current_time:%Y-%m-%d %H:%M:%S}\n"
            f"当前时区：{getattr(context.timezone, 'key', context.timezone)}\n"
            f"当前用户原话：\n{user_text}"
        )
        return [
            {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    def _validated_candidate(
        self,
        tool_name: str,
        args: dict[str, Any],
        context: TodoToolContext,
    ) -> TodoProposalCandidate | None:
        """Validate one LLM call and bind its target to stable revisions.

        The method intentionally refuses malformed or unresolvable candidates
        instead of filling missing values.  A question is safer than a
        plausible-looking write assembled from incomplete input.
        """

        spec = TOOL_SPECS_BY_NAME.get(tool_name)
        if spec is None or not isinstance(args, dict):
            return None
        try:
            validate_json_schema(args, spec.parameters)
            normalized_args, snapshots = _normalize_and_snapshot_candidate(
                self.store, tool_name, args, context
            )
        except (ToolValidationError, ToolExecutionStop, ValueError, TypeError):
            return None
        return TodoProposalCandidate(0, tool_name, normalized_args, tuple(snapshots))


class TodoProposalResolver:
    """Convert one later user reply into an allowed proposal state action.

    Inputs and outputs:
        Receives only text, the server-loaded proposal and trusted context;
        returns one of ``accept(option_id)``, ``reject``, ``replace(new_text)``
        or ``clarify``.  It has no store mutation or executor dependency.
    Fallback:
        Deterministic words are handled first.  Ambiguous language is sent to
        the LLM in a constrained tool-call Schema; failures become ``clarify``.
    """

    def __init__(self, config: dict[str, Any], client: Any | None = None) -> None:
        self.config = config
        self.client = client or OpenAICompatibleChatClient(config)

    async def resolve(
        self,
        reply_text: str,
        proposal: TodoProposal,
        context: TodoToolContext,
    ) -> TodoProposalResolution:
        """Classify without executing, persisting, or accepting a candidate."""

        normalized = " ".join((reply_text or "").strip().split())
        if not normalized:
            return TodoProposalResolution("clarify")
        direct = _deterministic_resolution(normalized, proposal)
        if direct is not None:
            return direct
        try:
            choice = await self.client.complete_with_tools(
                self._build_messages(normalized, proposal, context),
                [_resolution_tool_definition()],
            )
        except Exception:
            return TodoProposalResolution("clarify")
        calls = list(getattr(choice, "tool_calls", []) or [])
        if not calls:
            return TodoProposalResolution("clarify")
        call = calls[0]
        if getattr(call, "name", "") != "resolve_todo_proposal_reply":
            return TodoProposalResolution("clarify")
        return _validated_resolution(getattr(call, "arguments", {}), proposal)

    def _build_messages(
        self,
        reply_text: str,
        proposal: TodoProposal,
        context: TodoToolContext,
    ) -> list[dict[str, str]]:
        """Supply only the current user's proposal context to the classifier."""

        choices = "\n".join(
            f"[{candidate.option_id}] {_describe_candidate(candidate)}"
            for candidate in proposal.candidates
        ) or "（当前是在补充信息，没有可接受候选）"
        prompt = (
            f"原请求：{proposal.source_text}\n"
            f"候选：\n{choices}\n"
            f"用户最新回复：{reply_text}\n"
            f"当前时区：{getattr(context.timezone, 'key', context.timezone)}"
        )
        return [
            {"role": "system", "content": _RESOLVER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]


class TodoProposalExecutionGate:
    """Revalidate a claimed candidate immediately before its only real write.

    The caller must first use ``TodoStore.claim_proposal_for_execution``.  The
    gate repeats tool Schema validation, proposal expiry checks and stable
    target revision/status checks before delegating to ``TodoToolExecutor``.
    A candidate never receives trust merely because it was validated at plan
    time.  Permanent delete is not special-cased around the executor: its
    existing two-phase token state machine remains authoritative.
    """

    def __init__(self, store: TodoStore) -> None:
        self.store = store

    def execute(
        self,
        proposal: TodoProposal,
        candidate: TodoProposalCandidate,
        context: TodoToolContext,
    ) -> ToolResult:
        """Return a real tool result or a safe terminal error without writes."""

        if proposal.status != PROPOSAL_ACCEPTED or proposal.expires_at <= context.now:
            return ToolResult(False, "error", "该待确认操作已过期，未执行任何待办操作", {})
        spec = TOOL_SPECS_BY_NAME.get(candidate.tool_name)
        if spec is None:
            return ToolResult(False, "error", "候选操作已失效，未执行任何待办操作", {})
        try:
            validate_json_schema(candidate.arguments, spec.parameters)
        except ToolValidationError:
            return ToolResult(False, "error", "候选操作参数校验失败，未执行任何待办操作", {})
        if not self._targets_unchanged(candidate, context):
            return ToolResult(False, "error", "待办目标已变化，请重新查看或重新说明操作", {})
        # This is intentionally the only call site in the proposal path that
        # crosses from a proposal to a real database mutation.
        return TodoToolExecutor(self.store, context).execute(candidate.tool_name, candidate.arguments)

    def _targets_unchanged(
        self,
        candidate: TodoProposalCandidate,
        context: TodoToolContext,
    ) -> bool:
        """Confirm every saved stable target ID still has its planned revision."""

        for snapshot in candidate.target_snapshots:
            history_id = snapshot.get("history_id")
            revision = snapshot.get("revision")
            status = snapshot.get("status")
            if not isinstance(history_id, str) or not isinstance(revision, int) or not isinstance(status, str):
                return False
            current = self.store.find_by_history_id(context.user_id, history_id, None)
            if current is None or current.revision != revision or current.status != status:
                return False
        return True


def render_proposal(proposal: TodoProposal) -> str:
    """Render backend-owned, numbered user choices from validated arguments.

    Model prose never provides the operation wording when candidates exist.
    The rephrase item is deliberately numbered after at most three candidates
    so the resolver can accept an explicit option number safely.
    """

    if proposal.candidates:
        if len(proposal.candidates) == 1:
            candidate = proposal.candidates[0]
            prompt = f"是要{_describe_candidate(candidate)}吗？"
            accept = f"是，{_candidate_action_label(candidate)}"
            return f"{prompt}\n[{candidate.option_id}] {accept}\n[2] 否，重新说明"
        rows = ["我理解为以下其中一种操作，请选择："]
        rows.extend(
            f"[{candidate.option_id}] {_candidate_action_label(candidate)}"
            for candidate in proposal.candidates
        )
        rows.append(f"[{len(proposal.candidates) + 1}] 重新说明")
        return "\n".join(rows)
    question = proposal.question_text.strip() or "请补充待办的具体内容、编号或时间。"
    return f"{question}\n请直接补充信息，或回复“取消”结束本次操作。"


def _normalize_and_snapshot_candidate(
    store: TodoStore,
    tool_name: str,
    args: dict[str, Any],
    context: TodoToolContext,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Normalize references and bind affected targets to stable snapshots.

    The stored arguments remain public tool arguments, while the parallel
    snapshot carries history ID + revision + allowed current status.  Numbers
    are normalized now so a process restart cannot lose the in-memory
    “previous item” reference needed by an accepted proposal.
    """

    normalized = dict(args)
    snapshots: list[dict[str, Any]] = []
    if tool_name in {"create_todo", "list_todos"}:
        return normalized, snapshots

    if tool_name in {"get_todo", "edit_todo", "shift_todo_time", "cancel_todo"}:
        number = _candidate_number(normalized, context)
        normalized["number"] = number
        normalized.pop("reference", None)
        allowed = None if tool_name == "get_todo" else (STATUS_OPEN,)
        item = store.find_by_no(context.scope, context.group_id, context.user_id, number, allowed)
        if item is None:
            raise ValueError("target is not visible in the required state")
        snapshots.append(_target_snapshot(item))
        return normalized, snapshots

    if tool_name == "complete_todos":
        numbers = _candidate_numbers(normalized, context)
        normalized["numbers"] = numbers
        normalized.pop("number", None)
        normalized.pop("reference", None)
        for number in numbers:
            item = store.find_by_no(context.scope, context.group_id, context.user_id, number, (STATUS_OPEN,))
            if item is None:
                raise ValueError("completion target is not open")
            snapshots.append(_target_snapshot(item))
        return normalized, snapshots

    if tool_name == "merge_todos":
        numbers = [int(number) for number in normalized.get("numbers", [])]
        if len(set(numbers)) < 2:
            raise ValueError("merge needs two distinct items")
        for number in numbers:
            item = store.find_by_no(context.scope, context.group_id, context.user_id, number, (STATUS_OPEN,))
            if item is None:
                raise ValueError("merge target is not open")
            snapshots.append(_target_snapshot(item))
        return normalized, snapshots

    if tool_name == "restore_todos":
        history_ids = _candidate_history_ids(normalized)
        normalized["history_ids"] = history_ids
        normalized.pop("history_id", None)
        for history_id in history_ids:
            item = store.find_by_history_id(context.user_id, history_id, (STATUS_DONE, STATUS_DELETED))
            if item is None:
                raise ValueError("restore target is not a visible history item")
            snapshots.append(_target_snapshot(item))
        return normalized, snapshots

    if tool_name == "delete_todos":
        history_ids = _candidate_history_ids(normalized)
        normalized["history_ids"] = history_ids
        normalized.pop("history_id", None)
        for history_id in history_ids:
            item = store.find_by_history_id(context.user_id, history_id, None)
            if item is None:
                raise ValueError("delete target is not visible")
            snapshots.append(_target_snapshot(item))
        return normalized, snapshots
    raise ValueError("unsupported proposal tool")


def _candidate_number(args: dict[str, Any], context: TodoToolContext) -> int:
    value = args.get("number")
    if isinstance(value, int) and value > 0:
        return value
    reference = args.get("reference")
    if isinstance(reference, str):
        normalized = reference.strip()
        if normalized.isdigit() and int(normalized) > 0:
            return int(normalized)
        if normalized in REFERENCE_WORDS and context.last_todo_no is not None:
            return int(context.last_todo_no)
    raise ValueError("missing stable visible number")


def _candidate_numbers(args: dict[str, Any], context: TodoToolContext) -> list[int]:
    values = args.get("numbers")
    if isinstance(values, list) and values:
        numbers = list(dict.fromkeys(int(value) for value in values if isinstance(value, int) and value > 0))
        if numbers:
            return numbers
    return [_candidate_number(args, context)]


def _candidate_history_ids(args: dict[str, Any]) -> list[str]:
    values = args.get("history_ids")
    if isinstance(values, list) and values:
        ids = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        if ids:
            return ids
    value = args.get("history_id")
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    raise ValueError("missing history ID")


def _target_snapshot(item: TodoReminder) -> dict[str, Any]:
    """Create a public stable target snapshot for plan-to-execute revalidation."""

    return {
        "history_id": item.history_id,
        "revision": item.revision,
        "status": item.status,
        "number": item.todo_no,
        "title": item.title,
    }


def _format_visible_todos(items: list[TodoReminder]) -> str:
    """Provide only user-visible summaries, never database primary keys."""

    if not items:
        return "无"
    return "\n".join(
        f"[{item.todo_no}] {item.title} | 状态={item.status} | 历史ID={item.history_id}"
        for item in items
    )


def _deterministic_resolution(text: str, proposal: TodoProposal) -> TodoProposalResolution | None:
    lowered = text.lower()
    if lowered in {"否", "不", "不要", "取消", "算了", "拒绝"}:
        return TodoProposalResolution("reject")
    if text.startswith("不，是") or text.startswith("不对，是") or text.startswith("改成"):
        replacement = text.split("是", 1)[-1].strip() if "是" in text else text
        return TodoProposalResolution("replace", new_text=replacement or text)
    if proposal.candidates:
        option_ids = {candidate.option_id for candidate in proposal.candidates}
        rephrase_id = len(proposal.candidates) + 1
        selected = _explicit_option_number(text)
        if selected is not None:
            if selected in option_ids:
                return TodoProposalResolution("accept", option_id=selected)
            if selected == rephrase_id:
                # The rephrase choice must close the old intent; carrying its
                # opaque number into a new LLM prompt would be misleading.
                return TodoProposalResolution("reject")
        if lowered in {"是", "好的", "好", "确认", "确定", "可以", "行"} and len(proposal.candidates) == 1:
            return TodoProposalResolution("accept", option_id=proposal.candidates[0].option_id)
    return None


def _explicit_option_number(text: str) -> int | None:
    """Parse a human-visible option number without treating free text as one."""

    normalized = text.strip().lower()
    if normalized.isdigit():
        return int(normalized)
    for prefix in ("选", "选择", "第"):
        if normalized.startswith(prefix):
            suffix = normalized[len(prefix) :].strip()
            if suffix.endswith("个") or suffix.endswith("项"):
                suffix = suffix[:-1].strip()
            if suffix.isdigit():
                return int(suffix)
    return None


def _resolution_tool_definition() -> dict[str, Any]:
    """Define the resolver's deliberately tiny structured-output contract."""

    return {
        "type": "function",
        "function": {
            "name": "resolve_todo_proposal_reply",
            "description": "分类待确认待办的用户回复；绝不执行操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["accept", "reject", "replace", "clarify"]},
                    "option_id": {"type": ["integer", "null"], "minimum": 1},
                    "new_text": {"type": ["string", "null"]},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    }


def _validated_resolution(args: Any, proposal: TodoProposal) -> TodoProposalResolution:
    """Reject malformed model classifications instead of guessing an action."""

    if not isinstance(args, dict):
        return TodoProposalResolution("clarify")
    try:
        validate_json_schema(args, _resolution_tool_definition()["function"]["parameters"])
    except ToolValidationError:
        return TodoProposalResolution("clarify")
    action = args.get("action")
    if action == "accept":
        option_id = args.get("option_id")
        if isinstance(option_id, int) and any(item.option_id == option_id for item in proposal.candidates):
            return TodoProposalResolution("accept", option_id=option_id)
        return TodoProposalResolution("clarify")
    if action == "reject":
        return TodoProposalResolution("reject")
    if action == "replace":
        text = args.get("new_text")
        if isinstance(text, str) and text.strip():
            return TodoProposalResolution("replace", new_text=text.strip())
    return TodoProposalResolution("clarify")


def _describe_candidate(candidate: TodoProposalCandidate) -> str:
    """Create a readable, backend-controlled description of a candidate."""

    args = candidate.arguments
    targets = candidate.target_snapshots
    if candidate.tool_name == "create_todo":
        return f"创建待办“{str(args.get('title', '')).strip()}”"
    if candidate.tool_name == "shift_todo_time":
        number = args.get("number", "?")
        direction = "推迟" if args.get("direction") == "later" else "提前"
        field = {"reminder_at": "提醒时间", "due_at": "截止时间", "both": "提醒和截止时间", "auto": "时间"}.get(args.get("field"), "时间")
        return f"把第 {number} 条待办的{field}{direction} {args.get('delta_minutes', '?')} 分钟"
    if candidate.tool_name == "edit_todo":
        return f"修改第 {args.get('number', '?')} 条待办"
    if candidate.tool_name == "complete_todos":
        numbers = args.get("numbers") or [args.get("number", "?")]
        return "完成第 " + "、".join(str(number) for number in numbers) + " 条待办"
    if candidate.tool_name == "cancel_todo":
        return f"取消第 {args.get('number', '?')} 条待办"
    if candidate.tool_name == "merge_todos":
        return "合并第 " + "、".join(str(number) for number in args.get("numbers", [])) + " 条待办"
    if candidate.tool_name == "get_todo":
        return f"查看第 {args.get('number', '?')} 条待办"
    if candidate.tool_name == "list_todos":
        return "查看待办列表"
    if candidate.tool_name == "restore_todos":
        return "恢复待办“" + "、".join(str(item.get("title", item.get("history_id", ""))) for item in targets) + "”"
    if candidate.tool_name == "delete_todos":
        return "永久删除待办“" + "、".join(str(item.get("title", item.get("history_id", ""))) for item in targets) + "”"
    return "执行待办操作"


def _candidate_action_label(candidate: TodoProposalCandidate) -> str:
    description = _describe_candidate(candidate)
    return description[0].upper() + description[1:] if description else "确认操作"


def _looks_like_unsafe_success_claim(text: str) -> bool:
    """Do not show a model's unverified success statement as a proposal prompt."""

    return any(word in text for word in ("已完成", "已创建", "已添加", "已删除", "已经完成", "已经创建"))
