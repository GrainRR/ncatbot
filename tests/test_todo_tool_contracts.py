"""Todo 工具注册表、执行器和 Tool Loop 的契约测试。"""

from __future__ import annotations

import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.todo_reminder.llm.openai_chat import ChatCompletionChoice, ChatToolCall
from plugins.todo_reminder.llm.tool_loop import TodoToolLoop
from plugins.todo_reminder.todo_manage_tools import (
    TODO_TOOL_NAMES,
    TOOL_ORDER,
    TodoToolContext,
    TodoToolExecutor,
    ToolResult,
    openai_tool_definitions,
)
from plugins.todo_reminder.todo_manage_tools.contracts import ToolResult as ContractToolResult
from plugins.todo_reminder.todo_manage_tools.registry import TOOL_SPECS
from plugins.todo_reminder.todo_store import TodoStore


NOW = 2_000_000_000


class FakeClient:
    """返回固定工具调用的最小异步 LLM 替身。"""

    async def complete_with_tools(self, messages, tools):
        self.messages = messages
        self.tools = tools
        return ChatCompletionChoice(
            content="",
            tool_calls=[ChatToolCall(name="list_todos", arguments={}, call_id="list")],
        )


class TodoToolContractsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = TodoStore(Path(self.temp_dir.name) / "todos.sqlite")
        self.store.init()
        self.context = TodoToolContext(
            scope="private",
            group_id=None,
            user_id="owner",
            now=NOW,
            timezone=ZoneInfo("Asia/Shanghai"),
        )
        self.executor = TodoToolExecutor(self.store, self.context)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_registry_is_the_single_ordered_source_of_tool_contracts(self) -> None:
        self.assertEqual(TOOL_ORDER, TODO_TOOL_NAMES)
        self.assertEqual(TOOL_ORDER, tuple(spec.name for spec in TOOL_SPECS))
        definitions = openai_tool_definitions()
        self.assertEqual(TOOL_ORDER, tuple(item["function"]["name"] for item in definitions))
        self.assertEqual(definitions, self.executor.tool_definitions)
        self.assertIs(ToolResult, ContractToolResult)

    def test_all_tools_reject_unknown_parameters(self) -> None:
        for tool_name in TODO_TOOL_NAMES:
            with self.subTest(tool=tool_name):
                result = self.executor.execute(tool_name, {"unexpected": True})
                self.assertFalse(result.ok)
                self.assertEqual("error", result.status)

    def test_all_ten_tools_execute_through_the_registry(self) -> None:
        created = self.executor.execute(
            "create_todo", {"title": "first", "reminder_at": "2033-05-18 11:34:20"}
        )
        self.assertTrue(created.ok)
        first = created.data["item"]

        self.assertTrue(self.executor.execute("list_todos", {}).ok)
        self.assertTrue(self.executor.execute("get_todo", {"number": first["number"]}).ok)
        self.assertTrue(self.executor.execute("edit_todo", {"number": first["number"], "title": "edited"}).ok)
        self.assertTrue(
            self.executor.execute(
                "shift_todo_time",
                {"number": first["number"], "field": "reminder_at", "direction": "later", "delta_minutes": 1},
            ).ok
        )
        self.assertTrue(self.executor.execute("complete_todos", {"number": first["number"]}).ok)
        done = self.store.list_completed("private", None, "owner")[0]
        self.assertTrue(self.executor.execute("restore_todos", {"history_id": done.history_id}).ok)
        self.assertTrue(self.executor.execute("cancel_todo", {"number": first["number"]}).ok)

        deleted = self.store.find_by_history_id("owner", done.history_id)
        confirmation = self.executor.execute("delete_todos", {"history_id": deleted.history_id, "confirmed": True})
        self.assertEqual("confirm", confirmation.status)

        second = self.executor.execute("create_todo", {"title": "second"}).data["item"]
        third = self.executor.execute("create_todo", {"title": "third"}).data["item"]
        merged = self.executor.execute("merge_todos", {"numbers": [second["number"], third["number"]]})
        self.assertTrue(merged.ok)

    def test_clarify_and_expected_storage_exceptions_are_normalized(self) -> None:
        clarify = self.executor.execute("get_todo", {})
        self.assertEqual("clarify", clarify.status)
        self.assertEqual("error", self.executor.execute("unknown", {}).status)

        first = self.executor.execute("create_todo", {"title": "first"}).data["item"]
        second = self.executor.execute("create_todo", {"title": "second"}).data["item"]
        with self.store._connect() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_executor_merge
                BEFORE UPDATE OF status ON todo_reminders
                WHEN NEW.status = 'deleted'
                BEGIN SELECT RAISE(ABORT, 'forced failure'); END;
                """
            )
        failed = self.executor.execute(
            "merge_todos", {"numbers": [first["number"], second["number"]]}
        )
        self.assertEqual("storage_error", failed.data["code"])
        self.assertEqual("first", self.store.find_pending_by_no("private", None, "owner", first["number"]).title)

    def test_tool_loop_uses_the_package_executor_and_registry(self) -> None:
        client = FakeClient()
        loop = TodoToolLoop({}, self.store, client=client)
        response = __import__("asyncio").run(loop.run("查看待办", self.context))
        self.assertTrue(response.tool_results[0].ok)
        self.assertEqual(TOOL_ORDER, tuple(tool["function"]["name"] for tool in client.tools))


if __name__ == "__main__":
    unittest.main()
