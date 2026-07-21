"""Regression tests for the persisted confirm-before-execute Todo flow."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.todo_reminder.llm.openai_chat import ChatCompletionChoice, ChatToolCall
from plugins.todo_reminder.proposals import (
    TodoProposalExecutionGate,
    TodoProposalPlanner,
    TodoProposalResolver,
    render_proposal,
)
from plugins.todo_reminder.todo_manage_tools import TodoToolContext
from plugins.todo_reminder.todo_store import (
    PROPOSAL_EXECUTED,
    STATUS_OPEN,
    TodoProposalCandidate,
    TodoReminderDraft,
    TodoStore,
)


NOW = 2_000_000_000


class FakeClient:
    """Minimal client double that records planning/resolution calls."""

    def __init__(self, choice: ChatCompletionChoice) -> None:
        self.choice = choice
        self.calls: list[tuple[object, object]] = []

    async def complete_with_tools(self, messages, tools):
        self.calls.append((messages, tools))
        return self.choice


def context(user_id: str = "owner") -> TodoToolContext:
    return TodoToolContext(
        scope="private",
        group_id=None,
        user_id=user_id,
        now=NOW,
        timezone=ZoneInfo("Asia/Shanghai"),
        user_text="test",
    )


def draft(title: str) -> TodoReminderDraft:
    return TodoReminderDraft(
        title=title,
        content=None,
        raw_text=title,
        remind_at=NOW + 3600,
        due_at=None,
        reminder_text=title,
        llm_json={},
    )


class TodoProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "todos.sqlite"
        self.store = TodoStore(self.path)
        self.store.init()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _proposal(self, candidates: tuple[TodoProposalCandidate, ...], ttl: int = 300):
        return self.store.create_proposal(
            "owner", "private", None, "source", "question", candidates, NOW, ttl
        )

    def test_planner_creates_candidate_without_any_todo_write(self) -> None:
        client = FakeClient(
            ChatCompletionChoice(
                "", [ChatToolCall("create_todo", {"title": "关窗户"})]
            )
        )
        planner = TodoProposalPlanner({}, self.store, client)

        plan = asyncio.run(planner.plan("关窗户", context()))

        self.assertEqual(1, len(plan.candidates))
        self.assertEqual("create_todo", plan.candidates[0].tool_name)
        self.assertEqual([], self.store.list_pending("private", None, "owner"))
        self.assertEqual(1, len(client.calls))

    def test_invalid_llm_tool_is_only_a_question_not_a_write(self) -> None:
        client = FakeClient(
            ChatCompletionChoice("请补充时间", [ChatToolCall("drop_database", {})])
        )
        plan = asyncio.run(TodoProposalPlanner({}, self.store, client).plan("做点事", context()))

        self.assertEqual((), plan.candidates)
        self.assertEqual("请补充时间", plan.question_text)
        self.assertEqual([], self.store.list_pending("private", None, "owner"))

    def test_planner_validates_all_mutating_candidate_shapes_without_mutating(self) -> None:
        """Every planned tool is a proposal; only the gate may mutate later."""

        done, first_open, second_open = self.store.create_many(
            "private", None, "owner", [draft("已完成"), draft("第一条"), draft("第二条")], NOW
        )
        self.store.complete(done.id, "owner")
        expected = [
            ("create_todo", {"title": "新待办"}),
            ("edit_todo", {"number": first_open.todo_no, "title": "已改标题"}),
            (
                "shift_todo_time",
                {
                    "number": first_open.todo_no,
                    "field": "reminder_at",
                    "direction": "later",
                    "delta_minutes": 20,
                },
            ),
            ("complete_todos", {"numbers": [first_open.todo_no]}),
            ("restore_todos", {"history_id": done.history_id}),
            (
                "merge_todos",
                {"numbers": [first_open.todo_no, second_open.todo_no]},
            ),
            ("delete_todos", {"history_id": done.history_id}),
        ]
        before = [
            (item.history_id, item.title, item.status, item.remind_at, item.revision)
            for item in self.store.list_by_status("private", None, "owner", None, 20)
        ]

        for tool_name, arguments in expected:
            with self.subTest(tool=tool_name):
                client = FakeClient(ChatCompletionChoice("", [ChatToolCall(tool_name, arguments)]))
                plan = asyncio.run(TodoProposalPlanner({}, self.store, client).plan("任意输入", context()))
                self.assertEqual(1, len(plan.candidates))
                self.assertEqual(tool_name, plan.candidates[0].tool_name)
                after = [
                    (item.history_id, item.title, item.status, item.remind_at, item.revision)
                    for item in self.store.list_by_status("private", None, "owner", None, 20)
                ]
                self.assertEqual(before, after)

    def test_candidate_accept_executes_once_and_persists_terminal_result(self) -> None:
        proposal = self._proposal(
            (TodoProposalCandidate(1, "create_todo", {"title": "关窗户"}),)
        )
        outcome, accepted, candidate = self.store.claim_proposal_for_execution(
            proposal.token, 1, "owner", "private", None, "message_id:1", NOW
        )
        self.assertEqual("accepted", outcome)
        result = TodoProposalExecutionGate(self.store).execute(accepted, candidate, context())
        self.assertTrue(result.ok)
        self.store.complete_proposal_execution(proposal.token, result.message, NOW)

        duplicate, duplicate_proposal, _ = self.store.claim_proposal_for_execution(
            proposal.token, 1, "owner", "private", None, "message_id:1", NOW
        )
        self.assertEqual("duplicate", duplicate)
        self.assertEqual(PROPOSAL_EXECUTED, duplicate_proposal.status)
        self.assertEqual(1, len(self.store.list_pending("private", None, "owner")))

    def test_expired_and_cross_session_proposals_never_claim(self) -> None:
        proposal = self._proposal(
            (TodoProposalCandidate(1, "create_todo", {"title": "关窗户"}),), ttl=10
        )
        cross, _, _ = self.store.claim_proposal_for_execution(
            proposal.token, 1, "intruder", "private", None, "event:1", NOW
        )
        self.assertEqual("mismatch", cross)
        expired, _, _ = self.store.claim_proposal_for_execution(
            proposal.token, 1, "owner", "private", None, "event:2", NOW + 10
        )
        self.assertEqual("expired", expired)
        self.assertEqual([], self.store.list_pending("private", None, "owner"))

        group_proposal = self.store.create_proposal(
            "owner",
            "group",
            "group-a",
            "source",
            "question",
            (TodoProposalCandidate(1, "create_todo", {"title": "群内待办"}),),
            NOW,
            300,
        )
        cross_group, _, _ = self.store.claim_proposal_for_execution(
            group_proposal.token, 1, "owner", "group", "group-b", "event:3", NOW
        )
        self.assertEqual("mismatch", cross_group)

    def test_target_revision_change_blocks_gate_before_real_execution(self) -> None:
        item = self.store.create_many("private", None, "owner", [draft("会议")], NOW)[0]
        candidate = TodoProposalCandidate(
            1,
            "shift_todo_time",
            {
                "number": item.todo_no,
                "field": "reminder_at",
                "direction": "later",
                "delta_minutes": 20,
            },
            ({"history_id": item.history_id, "revision": item.revision, "status": STATUS_OPEN},),
        )
        proposal = self._proposal((candidate,))
        self.store.update_fields(item.id, {"title": "已改名"}, STATUS_OPEN, "owner", NOW, True)
        _, accepted, claimed = self.store.claim_proposal_for_execution(
            proposal.token, 1, "owner", "private", None, "event:3", NOW
        )

        result = TodoProposalExecutionGate(self.store).execute(accepted, claimed, context())

        self.assertFalse(result.ok)
        self.assertIn("目标已变化", result.message)
        current = self.store.find_by_history_id("owner", item.history_id)
        self.assertEqual(NOW + 3600, current.remind_at)

    def test_resolver_and_renderer_keep_human_choice_separate_from_execution(self) -> None:
        candidate = TodoProposalCandidate(1, "create_todo", {"title": "关窗户"})
        proposal = self._proposal((candidate,))
        resolver = TodoProposalResolver({}, FakeClient(ChatCompletionChoice("", [])))

        resolution = asyncio.run(resolver.resolve("是", proposal, context()))

        self.assertEqual("accept", resolution.action)
        self.assertEqual(1, resolution.option_id)
        self.assertIn("是要创建待办“关窗户”吗？", render_proposal(proposal))
        self.assertEqual([], self.store.list_pending("private", None, "owner"))

    def test_resolver_can_classify_a_new_request_as_replace_without_execution(self) -> None:
        candidate = TodoProposalCandidate(1, "create_todo", {"title": "关窗户"})
        proposal = self._proposal((candidate,))
        client = FakeClient(
            ChatCompletionChoice(
                "",
                [
                    ChatToolCall(
                        "resolve_todo_proposal_reply",
                        {"action": "replace", "new_text": "十分钟后提醒我关窗户"},
                    )
                ],
            )
        )

        resolution = asyncio.run(TodoProposalResolver({}, client).resolve("改一下", proposal, context()))

        self.assertEqual("replace", resolution.action)
        self.assertEqual("十分钟后提醒我关窗户", resolution.new_text)
        self.assertEqual([], self.store.list_pending("private", None, "owner"))

    def test_deterministic_restatement_replaces_instead_of_accepting(self) -> None:
        candidate = TodoProposalCandidate(1, "create_todo", {"title": "关窗户"})
        proposal = self._proposal((candidate,))

        resolution = asyncio.run(
            TodoProposalResolver({}, FakeClient(ChatCompletionChoice("", []))).resolve(
                "不，是十分钟后提醒我关窗户", proposal, context()
            )
        )

        self.assertEqual("replace", resolution.action)
        self.assertEqual("十分钟后提醒我关窗户", resolution.new_text)
        self.assertEqual([], self.store.list_pending("private", None, "owner"))

    def test_multiple_candidates_are_capped_and_rephrase_is_numbered(self) -> None:
        candidates = tuple(
            TodoProposalCandidate(index, "create_todo", {"title": f"任务{index}"})
            for index in range(1, 4)
        )
        proposal = self._proposal(candidates)
        rendered = render_proposal(proposal)

        self.assertIn("[1]", rendered)
        self.assertIn("[3]", rendered)
        self.assertIn("[4] 重新说明", rendered)

    def test_explicit_second_candidate_is_selected_but_not_executed_by_resolver(self) -> None:
        proposal = self._proposal(
            (
                TodoProposalCandidate(1, "create_todo", {"title": "任务一"}),
                TodoProposalCandidate(2, "create_todo", {"title": "任务二"}),
            )
        )

        resolution = asyncio.run(
            TodoProposalResolver({}, FakeClient(ChatCompletionChoice("", []))).resolve(
                "选择 2", proposal, context()
            )
        )

        self.assertEqual("accept", resolution.action)
        self.assertEqual(2, resolution.option_id)
        self.assertEqual([], self.store.list_pending("private", None, "owner"))

    def test_active_proposal_survives_store_restart(self) -> None:
        proposal = self._proposal(
            (TodoProposalCandidate(1, "create_todo", {"title": "重启后仍在"}),)
        )
        restarted = TodoStore(self.path)
        restarted.init()

        active = restarted.get_active_proposal("owner", "private", None, NOW + 1)

        self.assertIsNotNone(active)
        self.assertEqual(proposal.token, active.token)
        self.assertEqual("create_todo", active.candidates[0].tool_name)

    def test_permanent_delete_candidate_still_stops_at_independent_confirmation(self) -> None:
        item = self.store.create_many("private", None, "owner", [draft("重要记录")], NOW)[0]
        candidate = TodoProposalCandidate(
            1,
            "delete_todos",
            {"history_ids": [item.history_id]},
            ({"history_id": item.history_id, "revision": item.revision, "status": STATUS_OPEN},),
        )
        proposal = self._proposal((candidate,))
        _, accepted, claimed = self.store.claim_proposal_for_execution(
            proposal.token, 1, "owner", "private", None, "event:delete", NOW
        )

        result = TodoProposalExecutionGate(self.store).execute(accepted, claimed, context())

        self.assertFalse(result.ok)
        self.assertEqual("confirm", result.status)
        self.assertIsNotNone(self.store.find_by_history_id("owner", item.history_id))



if __name__ == "__main__":
    unittest.main()
