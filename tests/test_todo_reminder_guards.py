"""待办关键安全约束的回归测试。"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.todo_reminder.todo_manage_tools.tools import TodoToolContext, TodoToolExecutor
from plugins.todo_reminder.todo_store import (
    ReminderTimeValidationError,
    STATUS_DONE,
    STATUS_OPEN,
    TodoReminderDraft,
    TodoStore,
)


NOW = 2_000_000_000


def draft(title: str, remind_at: int | None) -> TodoReminderDraft:
    """构造测试用待办草稿。"""

    return TodoReminderDraft(
        title=title,
        content=None,
        raw_text=title,
        remind_at=remind_at,
        due_at=None,
        reminder_text=title,
        llm_json={},
    )


class TodoReminderGuardsTests(unittest.TestCase):
    """覆盖确认令牌、历史 ID、事务和提醒时间约束。"""

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

    def _create(self, *titles: str):
        return self.store.create_many(
            "private",
            None,
            "owner",
            [draft(title, NOW + 3600 + index * 60) for index, title in enumerate(titles)],
            NOW,
            True,
        )

    def test_confirmation_token_rejects_direct_other_expired_and_changed_requests(self) -> None:
        item = self._create("delete me")[0]

        first = self.executor.execute(
            "delete_todos", {"history_id": item.history_id, "confirmed": True}
        )
        self.assertEqual("confirm", first.status)
        self.assertIsNotNone(self.store.find_by_history_id("owner", item.history_id))
        token = first.data["confirmation_token"]

        other = TodoToolExecutor(
            self.store,
            TodoToolContext("private", None, "other", NOW, ZoneInfo("Asia/Shanghai")),
        ).execute(
            "delete_todos", {"history_id": item.history_id, "confirmation_token": token}
        )
        self.assertEqual("confirmation_invalid", other.data["code"])

        self.store.update_fields(item.id, {"title": "changed"}, STATUS_OPEN, "owner", NOW, True)
        changed = self.executor.execute(
            "delete_todos", {"history_id": item.history_id, "confirmation_token": token}
        )
        self.assertEqual("confirmation_target_changed", changed.data["code"])
        self.assertIsNotNone(self.store.find_by_history_id("owner", item.history_id))

        fresh = self.executor.execute("delete_todos", {"history_id": item.history_id})
        expired = TodoToolExecutor(
            self.store,
            TodoToolContext("private", None, "owner", NOW + 301, ZoneInfo("Asia/Shanghai")),
        ).execute(
            "delete_todos",
            {"history_id": item.history_id, "confirmation_token": fresh.data["confirmation_token"]},
        )
        self.assertEqual("confirmation_expired", expired.data["code"])

    def test_history_id_targets_the_correct_reused_display_number(self) -> None:
        old = self._create("old")[0]
        self.assertTrue(self.executor.execute("complete_todos", {"number": old.todo_no}).ok)
        newer = self._create("new")[0]
        self.assertEqual(old.todo_no, newer.todo_no)

        restored = self.executor.execute("restore_todos", {"history_id": old.history_id})
        self.assertTrue(restored.ok)
        self.assertEqual(STATUS_OPEN, self.store.find_by_history_id("owner", old.history_id).status)
        self.assertEqual(STATUS_OPEN, self.store.find_by_history_id("owner", newer.history_id).status)
        self.assertNotEqual(
            self.store.find_by_history_id("owner", old.history_id).todo_no,
            self.store.find_by_history_id("owner", newer.history_id).todo_no,
        )

    def test_init_migrates_old_records_to_stable_history_ids(self) -> None:
        old_path = Path(self.temp_dir.name) / "old.sqlite"
        with sqlite3.connect(old_path) as connection:
            connection.executescript(
                """
                CREATE TABLE todo_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    todo_no INTEGER NOT NULL,
                    scope TEXT NOT NULL,
                    group_id TEXT,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT,
                    raw_text TEXT NOT NULL,
                    remind_at INTEGER NOT NULL,
                    due_at INTEGER,
                    reminder_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at INTEGER NOT NULL,
                    reminded_at INTEGER,
                    llm_json TEXT
                );
                CREATE TABLE todo_reminder_modes (
                    scope TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (scope, group_id, user_id)
                );
                """
            )
            connection.execute(
                """
                INSERT INTO todo_reminders
                    (todo_no, scope, user_id, title, raw_text, remind_at, reminder_text, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (1, "private", "owner", "legacy", "legacy", NOW + 1, "legacy", STATUS_DONE, NOW),
            )
        connection.close()

        migrated = TodoStore(old_path)
        migrated.init()
        item = migrated.find_by_no("private", None, "owner", 1, None)
        self.assertIsNotNone(item)
        self.assertEqual("H-000000000001", item.history_id)
        self.assertEqual(1, item.revision)

    def test_batch_mutations_roll_back_on_status_or_database_failure(self) -> None:
        first, second = self._create("first", "second")
        self.store.complete(first.id, "owner")
        self.assertIsNone(self.store.complete_many([first.id, second.id], "owner"))
        self.assertEqual(STATUS_OPEN, self.store.find_by_history_id("owner", second.history_id).status)

        first, second = self._create("merge first", "merge second")
        with self.store._connect() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_merge_cancel
                BEFORE UPDATE OF status ON todo_reminders
                WHEN NEW.status = 'deleted'
                BEGIN
                    SELECT RAISE(ABORT, 'forced database failure');
                END;
                """
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.merge_open_todos(
                [first.id, second.id],
                "owner",
                {"title": "merged", "remind_at": NOW + 3600},
                NOW,
                True,
            )
        self.assertEqual("merge first", self.store.find_by_history_id("owner", first.history_id).title)
        self.assertEqual(STATUS_OPEN, self.store.find_by_history_id("owner", second.history_id).status)

    def test_reminder_validation_prevents_writes_in_all_mutation_paths(self) -> None:
        with self.assertRaises(ReminderTimeValidationError):
            self.store.create_many(
                "private", None, "owner", [draft("past", NOW)], NOW, True
            )
        self.assertEqual([], self.store.list_pending("private", None, "owner"))

        item = self._create("future")[0]
        shifted = self.executor.execute(
            "shift_todo_time",
            {
                "number": item.todo_no,
                "field": "reminder_at",
                "direction": "earlier",
                "delta_minutes": 120,
            },
        )
        self.assertEqual("remind_at_not_future", shifted.data["code"])
        self.assertEqual(NOW + 3600, self.store.find_by_history_id("owner", item.history_id).remind_at)

        old = self.store.create_many(
            "private", None, "owner", [draft("old", NOW - 1)], NOW, False
        )[0]
        self.store.complete(old.id, "owner")
        restored = self.executor.execute("restore_todos", {"history_id": old.history_id})
        self.assertEqual("remind_at_not_future", restored.data["code"])
        self.assertEqual(STATUS_DONE, self.store.find_by_history_id("owner", old.history_id).status)


if __name__ == "__main__":
    unittest.main()
