"""Tests for Todo reminder message presentation."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.todo_reminder.todo_manage_tools.presentation import format_concise_reminder
from plugins.todo_reminder.todo_store import STATUS_OPEN, TodoReminder


class ConciseReminderPresentationTests(unittest.TestCase):
    def test_renders_the_required_three_line_layout(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")
        item = TodoReminder(
            id=1,
            history_id="H-1",
            todo_no=7,
            revision=1,
            scope="private",
            group_id=None,
            user_id="owner",
            title="关窗户",
            content=None,
            raw_text="半小时后提醒我关窗户",
            remind_at=int(datetime(2026, 7, 22, 0, 55, tzinfo=timezone).timestamp()),
            due_at=None,
            reminder_text="不应在简洁模式中使用",
            status=STATUS_OPEN,
            created_at=0,
            reminded_at=None,
            deletion_reason=None,
            llm_json=None,
        )

        self.assertEqual(
            "待办：[7]关窗户\n"
            "待办详情：半小时后提醒我关窗户\n"
            "提醒时间：2026-07-22 00:55",
            format_concise_reminder(item, timezone),
        )
