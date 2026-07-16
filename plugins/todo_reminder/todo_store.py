"""待办提醒插件的 SQLite 存储层。"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


TODO_DB_FILENAME = "todos.sqlite"
STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_DELETED = "deleted"
MODE_CONCISE = "concise"
MODE_CATGIRL = "catgirl"
OPERATION_PERMANENT_DELETE = "permanent_delete"


class ReminderTimeValidationError(ValueError):
    """提醒时间不符合当前业务规则。"""

    def __init__(self, remind_at: int, now: int) -> None:
        self.remind_at = int(remind_at)
        self.now = int(now)
        super().__init__("remind_at must be later than now")


class TodoConfirmationError(ValueError):
    """永久删除确认令牌无效、过期或不再匹配目标。"""

    def __init__(self, code: str, **data: Any) -> None:
        self.code = code
        self.data = data
        super().__init__(code)


@dataclass(frozen=True)
class TodoReminderDraft:
    """创建待办前的结构化草稿。

    只保存用户想创建的待办内容，还没有数据库主键、展示序号、来源范围、
    状态或创建时间。写入数据库后会转换为 TodoReminder。
    """

    title: str
    content: str | None
    raw_text: str
    remind_at: int | None
    due_at: int | None
    reminder_text: str
    llm_json: dict[str, Any]


@dataclass(frozen=True)
class TodoReminder:
    """已持久化的待办提醒记录。

    表示数据库中已经存在的一条完整待办，包含数据库主键、展示序号、
    来源范围、状态、创建时间和提醒状态等持久化字段。
    """

    id: int
    history_id: str
    todo_no: int
    revision: int
    scope: str
    group_id: str | None
    user_id: str
    title: str
    content: str | None
    raw_text: str
    remind_at: int | None
    due_at: int | None
    reminder_text: str
    status: str
    created_at: int
    reminded_at: int | None
    llm_json: str | None


@dataclass(frozen=True)
class PermanentDeleteConfirmation:
    """一次永久删除确认请求的持久化结果。"""

    token: str
    target_history_ids: tuple[str, ...]
    expires_at: int


class TodoStore:
    """负责待办提醒的 SQLite 建表、查询和状态更新。"""

    def __init__(self, db_path: Path) -> None:
        """创建待办存储对象。

        Args:
            db_path: SQLite 数据库文件路径。
        """

        self.db_path = Path(db_path)

    def init(self) -> None:
        """初始化数据库目录、数据表和索引。"""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS todo_reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    history_id TEXT NOT NULL UNIQUE,
                    todo_no INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    scope TEXT NOT NULL,
                    group_id TEXT,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT,
                    raw_text TEXT NOT NULL,
                    remind_at INTEGER,
                    due_at INTEGER,
                    reminder_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at INTEGER NOT NULL,
                    reminded_at INTEGER,
                    llm_json TEXT
                );

                CREATE TABLE IF NOT EXISTS todo_reminder_modes (
                    user_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id)
                );

                CREATE TABLE IF NOT EXISTS todo_group_configs (
                    group_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS todo_operation_confirmations (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    operation_type TEXT NOT NULL,
                    target_snapshot TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )
            self._ensure_columns(conn)
            self._ensure_mode_table(conn)
            self._ensure_confirmation_table(conn)
            self._ensure_indexes(conn)

    def count_pending(self, scope: str, group_id: str | None, user_id: str) -> int:
        """统计指定范围内某个用户的未完成待办数量。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。

        Returns:
            状态为 `open` 的待办数量。
        """

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM todo_reminders
                WHERE user_id = ?
                  AND status = ?
                """,
                (user_id, STATUS_OPEN),
            ).fetchone()
        return int(row[0] if row else 0)

    def create_many(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        drafts: list[TodoReminderDraft],
        now: int,
        reject_past_reminder: bool = True,
    ) -> list[TodoReminder]:
        """在同一个事务中创建多条待办提醒记录。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            drafts: 已通过 LLM 解析和校验的待办草稿列表。
            now: 创建时间的 Unix 秒级时间戳。

        Returns:
            按创建顺序返回的完整待办记录列表。

        Raises:
            ValueError: drafts 为空时抛出。
        """

        if not drafts:
            raise ValueError("drafts cannot be empty")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            used_todo_numbers = self._used_open_todo_numbers(conn, user_id)
            todo_ids: list[int] = []
            for draft in drafts:
                validate_remind_at(draft.remind_at, now, reject_past_reminder)
                todo_no = _first_available_todo_no(used_todo_numbers)
                cursor = conn.execute(
                    """
                    INSERT INTO todo_reminders (
                        history_id,
                        todo_no,
                        revision,
                        scope,
                        group_id,
                        user_id,
                        title,
                        content,
                        raw_text,
                        remind_at,
                        due_at,
                        reminder_text,
                        status,
                        created_at,
                        reminded_at,
                        llm_json
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        _new_history_id(),
                        todo_no,
                        scope,
                        group_id,
                        user_id,
                        draft.title,
                        draft.content,
                        draft.raw_text,
                        _optional_int(draft.remind_at),
                        _optional_int(draft.due_at),
                        draft.reminder_text,
                        STATUS_OPEN,
                        int(now),
                        json.dumps(draft.llm_json, ensure_ascii=False),
                    ),
                )
                todo_ids.append(int(cursor.lastrowid))
                used_todo_numbers.add(todo_no)

            placeholders = ",".join("?" for _ in todo_ids)
            rows = conn.execute(
                f"SELECT * FROM todo_reminders WHERE id IN ({placeholders}) ORDER BY id",
                todo_ids,
            ).fetchall()
        if len(rows) != len(todo_ids):
            raise RuntimeError("todo disappeared after insert")
        todos_by_id = {int(row["id"]): _row_to_todo(row) for row in rows}
        return [todos_by_id[todo_id] for todo_id in todo_ids]

    def list_pending(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        limit: int = 20,
    ) -> list[TodoReminder]:
        """列出指定范围内某个用户的未完成待办。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            limit: 最多返回多少条。

        Returns:
            按提醒时间升序排列；没有提醒时间的待办排在最后。
        """

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM todo_reminders
                WHERE user_id = ?
                  AND status = ?
                ORDER BY remind_at IS NULL, remind_at, id
                LIMIT ?
                """,
                (user_id, STATUS_OPEN, int(limit)),
            ).fetchall()
        return [_row_to_todo(row) for row in rows]

    def list_by_status(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        status: str | None = STATUS_OPEN,
        limit: int = 20,
    ) -> list[TodoReminder]:
        """列出指定范围内某个用户的待办。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            status: 待办状态；传入 None 时返回所有状态。
            limit: 最多返回多少条。

        Returns:
            当前用户在当前范围内的待办列表。
        """

        where_status = ""
        params: list[Any] = [user_id]
        if status is not None:
            where_status = "AND status = ?"
            params.append(status)
        params.append(int(limit))

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM todo_reminders
                WHERE user_id = ?
                  {where_status}
                ORDER BY
                  CASE status
                    WHEN 'open' THEN 0
                    WHEN 'done' THEN 1
                    WHEN 'deleted' THEN 2
                    ELSE 3
                  END,
                  remind_at IS NULL,
                  remind_at,
                  created_at DESC,
                  id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_todo(row) for row in rows]

    def list_completed(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        limit: int = 20,
    ) -> list[TodoReminder]:
        """列出指定范围内某个用户的已完成待办。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            limit: 最多返回多少条。

        Returns:
            状态为 `done` 的待办列表。
        """

        return self.list_by_status(scope, group_id, user_id, STATUS_DONE, limit)

    def find_pending_by_no(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        todo_no: int,
    ) -> TodoReminder | None:
        """按当前范围内的待办序号查找一条未完成待办。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            todo_no: 当前范围内展示给用户的待办序号。

        Returns:
            匹配到的未完成待办；没有匹配时返回 None。
        """

        if todo_no <= 0:
            return None
        return self.find_by_no(scope, group_id, user_id, todo_no, (STATUS_OPEN,))

    def find_by_no(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        todo_no: int,
        statuses: Sequence[str] | None = None,
    ) -> TodoReminder | None:
        """按用户可见编号查找当前范围内的一条待办。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            todo_no: 当前范围内展示给用户的待办序号。
            statuses: 允许匹配的状态；传入 None 时允许所有状态。

        Returns:
            匹配到的待办；没有匹配时返回 None。
        """

        if todo_no <= 0:
            return None

        status_sql = ""
        params: list[Any] = [user_id, int(todo_no)]
        if statuses is not None:
            if not statuses:
                return None
            placeholders = ",".join("?" for _ in statuses)
            status_sql = f"AND status IN ({placeholders})"
            params.extend(statuses)

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM todo_reminders
                WHERE user_id = ?
                  AND todo_no = ?
                  {status_sql}
                ORDER BY
                  CASE status
                    WHEN 'open' THEN 0
                    WHEN 'done' THEN 1
                    WHEN 'deleted' THEN 2
                    ELSE 3
                  END,
                  created_at DESC,
                  id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return _row_to_todo(row) if row is not None else None

    def find_by_history_id(
        self,
        user_id: str,
        history_id: str,
        statuses: Sequence[str] | None = None,
    ) -> TodoReminder | None:
        """按不可复用的用户可见历史 ID 查找待办。"""

        history_id = str(history_id).strip()
        if not history_id:
            return None
        status_sql = ""
        params: list[Any] = [str(user_id), history_id]
        if statuses is not None:
            if not statuses:
                return None
            placeholders = ",".join("?" for _ in statuses)
            status_sql = f"AND status IN ({placeholders})"
            params.extend(statuses)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT *
                FROM todo_reminders
                WHERE user_id = ?
                  AND history_id = ?
                  {status_sql}
                """,
                params,
            ).fetchone()
        return _row_to_todo(row) if row is not None else None

    def find_by_history_ids(
        self,
        user_id: str,
        history_ids: Sequence[str],
        statuses: Sequence[str] | None = None,
    ) -> list[TodoReminder]:
        """按输入顺序批量查找稳定历史 ID，对缺失目标不作猜测。"""

        normalized_ids = _unique_history_ids(history_ids)
        if not normalized_ids:
            return []
        placeholders = ",".join("?" for _ in normalized_ids)
        status_sql = ""
        params: list[Any] = [str(user_id), *normalized_ids]
        if statuses is not None:
            if not statuses:
                return []
            status_placeholders = ",".join("?" for _ in statuses)
            status_sql = f"AND status IN ({status_placeholders})"
            params.extend(statuses)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM todo_reminders
                WHERE user_id = ?
                  AND history_id IN ({placeholders})
                  {status_sql}
                """,
                params,
            ).fetchall()
        by_history_id = {str(row["history_id"]): _row_to_todo(row) for row in rows}
        return [by_history_id[history_id] for history_id in normalized_ids if history_id in by_history_id]

    def complete_many(self, todo_ids: Sequence[int], user_id: str) -> list[TodoReminder] | None:
        """原子完成多条待办；任一目标失效则全部不写入。"""

        return self._transition_many(todo_ids, str(user_id), STATUS_OPEN, STATUS_DONE)

    def restore_many(
        self,
        todo_ids: Sequence[int],
        user_id: str,
        now: int,
        reject_past_reminder: bool,
    ) -> list[TodoReminder] | None:
        """原子恢复多条历史待办，并在事务内复核提醒时间。"""

        target_ids = _unique_ints(todo_ids)
        if not target_ids:
            return []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            items = self._load_exact_targets(conn, target_ids, str(user_id))
            if items is None or any(item.status not in {STATUS_DONE, STATUS_DELETED} for item in items):
                return None
            for item in items:
                validate_remind_at(item.remind_at, now, reject_past_reminder)

            used_numbers = self._used_open_todo_numbers(conn, str(user_id))
            restored_ids: list[int] = []
            for item in items:
                todo_no = item.todo_no
                if todo_no in used_numbers:
                    todo_no = _first_available_todo_no(used_numbers)
                used_numbers.add(todo_no)
                cursor = conn.execute(
                    """
                    UPDATE todo_reminders
                    SET status = ?, reminded_at = NULL, todo_no = ?, revision = revision + 1
                    WHERE id = ?
                      AND user_id = ?
                      AND status IN (?, ?)
                    """,
                    (
                        STATUS_OPEN,
                        todo_no,
                        item.id,
                        str(user_id),
                        STATUS_DONE,
                        STATUS_DELETED,
                    ),
                )
                if cursor.rowcount != 1:
                    return None
                restored_ids.append(item.id)
            return self._load_exact_targets(conn, restored_ids, str(user_id))

    def merge_open_todos(
        self,
        todo_ids: Sequence[int],
        user_id: str,
        updates: dict[str, Any],
        now: int,
        reject_past_reminder: bool,
    ) -> TodoReminder | None:
        """原子合并未完成待办：更新第一条并取消其余目标。"""

        target_ids = _unique_ints(todo_ids)
        if len(target_ids) < 2:
            raise ValueError("merge requires at least two todos")
        allowed_fields = {
            "title",
            "content",
            "raw_text",
            "remind_at",
            "due_at",
            "reminder_text",
        }
        unknown_fields = set(updates) - allowed_fields
        if unknown_fields:
            raise ValueError(f"unsupported todo fields: {sorted(unknown_fields)}")
        if not updates:
            raise ValueError("updates cannot be empty")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            items = self._load_exact_targets(conn, target_ids, str(user_id))
            if items is None or any(item.status != STATUS_OPEN for item in items):
                return None
            validate_remind_at(updates.get("remind_at"), now, reject_past_reminder)
            assignments = ", ".join(f"{field} = ?" for field in updates)
            cursor = conn.execute(
                f"""
                UPDATE todo_reminders
                SET {assignments}, revision = revision + 1
                WHERE id = ? AND user_id = ? AND status = ?
                """,
                [*updates.values(), items[0].id, str(user_id), STATUS_OPEN],
            )
            if cursor.rowcount != 1:
                return None
            for item in items[1:]:
                cursor = conn.execute(
                    """
                    UPDATE todo_reminders
                    SET status = ?, revision = revision + 1
                    WHERE id = ? AND user_id = ? AND status = ?
                    """,
                    (STATUS_DELETED, item.id, str(user_id), STATUS_OPEN),
                )
                if cursor.rowcount != 1:
                    return None
            row = conn.execute(
                "SELECT * FROM todo_reminders WHERE id = ?",
                (items[0].id,),
            ).fetchone()
        return _row_to_todo(row) if row is not None else None

    def complete(self, todo_id: int, user_id: str | None = None) -> TodoReminder | None:
        """把待办标记为已完成。

        Args:
            todo_id: 待办内部主键 ID。

        Returns:
            更新后的待办记录；待办不存在或状态不是 `open` 时返回 None。
        """

        return self._transition(todo_id, STATUS_OPEN, STATUS_DONE, user_id)

    def cancel(self, todo_id: int, user_id: str | None = None) -> TodoReminder | None:
        """把待办标记为已删除。

        Args:
            todo_id: 待办内部主键 ID。

        Returns:
            更新后的待办记录；待办不存在或状态不是 `open` 时返回 None。
        """

        return self._transition(todo_id, STATUS_OPEN, STATUS_DELETED, user_id)

    def restore(
        self,
        todo_id: int,
        user_id: str | None = None,
        now: int | None = None,
        reject_past_reminder: bool = True,
    ) -> TodoReminder | None:
        """把已完成或已删除的待办恢复为未完成。

        恢复时如果原展示编号已经被新的未完成待办占用，会重新分配当前范围内
        最小可用编号，避免破坏未完成待办编号唯一约束。

        Args:
            todo_id: 待办内部主键 ID。

        Returns:
            恢复后的待办记录；待办不存在或状态不是可恢复状态时返回 None。
        """

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM todo_reminders WHERE id = ?",
                (int(todo_id),),
            ).fetchone()
            if row is None:
                return None
            item = _row_to_todo(row)
            if user_id is not None and item.user_id != str(user_id):
                return None
            if item.status not in {STATUS_DONE, STATUS_DELETED}:
                return None
            validate_remind_at(
                item.remind_at,
                int(time.time()) if now is None else int(now),
                reject_past_reminder,
            )

            used_numbers = self._used_open_todo_numbers(conn, item.user_id)
            todo_no = item.todo_no
            if todo_no in used_numbers:
                todo_no = _first_available_todo_no(used_numbers)

            cursor = conn.execute(
                """
                UPDATE todo_reminders
                SET status = ?,
                    reminded_at = NULL,
                    todo_no = ?,
                    revision = revision + 1
                WHERE id = ?
                  AND user_id = ?
                  AND status IN (?, ?)
                """,
                (
                    STATUS_OPEN,
                    todo_no,
                    int(todo_id),
                    item.user_id,
                    STATUS_DONE,
                    STATUS_DELETED,
                ),
            )
            if cursor.rowcount == 0:
                return None
            restored = conn.execute(
                "SELECT * FROM todo_reminders WHERE id = ?",
                (int(todo_id),),
            ).fetchone()
        return _row_to_todo(restored) if restored is not None else None

    def delete_permanent(self, todo_id: int, user_id: str | None = None) -> bool:
        """永久删除一条待办记录。

        Args:
            todo_id: 待办内部主键 ID。

        Returns:
            确实删除了记录时返回 True；目标不存在时返回 False。
        """

        with self._connect() as conn:
            sql = "DELETE FROM todo_reminders WHERE id = ?"
            params: list[Any] = [int(todo_id)]
            if user_id is not None:
                sql += " AND user_id = ?"
                params.append(user_id)
            cursor = conn.execute(sql, params)
        return cursor.rowcount > 0

    def create_permanent_delete_confirmation(
        self,
        user_id: str,
        items: Sequence[TodoReminder],
        now: int,
        ttl_seconds: int,
    ) -> PermanentDeleteConfirmation:
        """持久化一次永久删除的确认令牌和目标版本快照。"""

        if not items:
            raise ValueError("confirmation targets cannot be empty")
        target_snapshot = [
            {"history_id": item.history_id, "revision": item.revision}
            for item in items
        ]
        token = f"del_{secrets.token_urlsafe(24)}"
        expires_at = int(now) + max(1, int(ttl_seconds))
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM todo_operation_confirmations WHERE expires_at <= ?",
                (int(now),),
            )
            conn.execute(
                """
                INSERT INTO todo_operation_confirmations (
                    token, user_id, operation_type, target_snapshot, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    str(user_id),
                    OPERATION_PERMANENT_DELETE,
                    json.dumps(target_snapshot, ensure_ascii=False, separators=(",", ":")),
                    expires_at,
                    int(now),
                ),
            )
        return PermanentDeleteConfirmation(
            token=token,
            target_history_ids=tuple(item.history_id for item in items),
            expires_at=expires_at,
        )

    def permanently_delete_confirmed(
        self,
        token: str,
        user_id: str,
        history_ids: Sequence[str],
        now: int,
    ) -> list[TodoReminder]:
        """原子验证确认令牌并永久删除其初始目标。"""

        target_history_ids = _unique_history_ids(history_ids)
        if not target_history_ids:
            raise TodoConfirmationError("confirmation_target_mismatch")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT target_snapshot, expires_at
                FROM todo_operation_confirmations
                WHERE token = ? AND user_id = ? AND operation_type = ?
                """,
                (str(token), str(user_id), OPERATION_PERMANENT_DELETE),
            ).fetchone()
            if row is None:
                raise TodoConfirmationError("confirmation_invalid")
            if int(row["expires_at"]) <= int(now):
                raise TodoConfirmationError("confirmation_expired")
            try:
                snapshot = json.loads(str(row["target_snapshot"]))
                expected_history_ids = tuple(str(entry["history_id"]) for entry in snapshot)
                expected_revisions = {
                    str(entry["history_id"]): int(entry["revision"])
                    for entry in snapshot
                }
            except (TypeError, ValueError, KeyError) as exc:
                raise TodoConfirmationError("confirmation_invalid") from exc
            if tuple(target_history_ids) != expected_history_ids:
                raise TodoConfirmationError(
                    "confirmation_target_mismatch",
                    expected_history_ids=list(expected_history_ids),
                )

            items = self._load_exact_history_targets(conn, target_history_ids, str(user_id))
            if items is None or any(
                item.revision != expected_revisions.get(item.history_id) for item in items
            ):
                raise TodoConfirmationError("confirmation_target_changed")
            placeholders = ",".join("?" for _ in target_history_ids)
            cursor = conn.execute(
                f"""
                DELETE FROM todo_reminders
                WHERE user_id = ? AND history_id IN ({placeholders})
                """,
                [str(user_id), *target_history_ids],
            )
            if cursor.rowcount != len(items):
                raise TodoConfirmationError("confirmation_target_changed")
            conn.execute(
                "DELETE FROM todo_operation_confirmations WHERE token = ?",
                (str(token),),
            )
        return items

    def update_fields(
        self,
        todo_id: int,
        updates: dict[str, Any],
        expected_status: str | None = STATUS_OPEN,
        user_id: str | None = None,
        now: int | None = None,
        reject_past_reminder: bool = True,
    ) -> TodoReminder | None:
        """更新一条待办的可编辑字段。

        Args:
            todo_id: 待办内部主键 ID。
            updates: 待更新字段和值，只允许标题、内容和时间等业务字段。
            expected_status: 允许更新的当前状态；传入 None 时不校验状态。

        Returns:
            更新后的待办记录；待办不存在或状态不匹配时返回 None。
        """

        allowed_fields = {
            "title",
            "content",
            "raw_text",
            "remind_at",
            "due_at",
            "reminder_text",
        }
        unknown_fields = set(updates) - allowed_fields
        if unknown_fields:
            raise ValueError(f"unsupported todo fields: {sorted(unknown_fields)}")
        if not updates:
            raise ValueError("updates cannot be empty")
        if "remind_at" in updates:
            if now is None:
                raise ValueError("now is required when updating remind_at")
            validate_remind_at(updates["remind_at"], now, reject_past_reminder)

        assignments = ", ".join(f"{field} = ?" for field in updates)
        params: list[Any] = list(updates.values())
        params.append(int(todo_id))
        user_sql = ""
        if user_id is not None:
            user_sql = "AND user_id = ?"
            params.append(user_id)
        status_sql = ""
        if expected_status is not None:
            status_sql = "AND status = ?"
            params.append(expected_status)

        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE todo_reminders
                SET {assignments}, revision = revision + 1
                WHERE id = ?
                  {user_sql}
                  {status_sql}
                """,
                params,
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM todo_reminders WHERE id = ?",
                (int(todo_id),),
            ).fetchone()
        return _row_to_todo(row) if row is not None else None

    def due_pending(self, now: int, limit: int = 20) -> list[TodoReminder]:
        """查询已经到期但尚未提醒的未完成待办。

        Args:
            now: 当前 Unix 秒级时间戳。
            limit: 最多返回多少条，避免单次扫描发送过多消息。

        Returns:
            按提醒时间升序排列的待办列表。
        """

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM todo_reminders
                WHERE status = ?
                  AND reminded_at IS NULL
                  AND remind_at IS NOT NULL
                  AND remind_at <= ?
                ORDER BY remind_at, id
                LIMIT ?
                """,
                (STATUS_OPEN, int(now), int(limit)),
            ).fetchall()
        return [_row_to_todo(row) for row in rows]

    def mark_reminded(self, todo_id: int, now: int, user_id: str | None = None) -> None:
        """标记待办已经提醒过，并自动软删除。

        Args:
            todo_id: 待办内部主键 ID。
            now: 提醒成功时的 Unix 秒级时间戳。
        """

        with self._connect() as conn:
            user_sql = ""
            params: list[Any] = [int(now), STATUS_DELETED, int(todo_id)]
            if user_id is not None:
                user_sql = "AND user_id = ?"
                params.append(user_id)
            params.append(STATUS_OPEN)
            conn.execute(
                f"""
                UPDATE todo_reminders
                SET reminded_at = ?,
                    status = ?,
                    revision = revision + 1
                WHERE id = ?
                  {user_sql}
                  AND reminded_at IS NULL
                  AND status = ?
                """,
                params,
            )

    def is_group_todo_enabled(self, group_id: str) -> bool:
        """读取群待办开关；没有配置的群默认关闭。"""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT enabled FROM todo_group_configs WHERE group_id = ?",
                (str(group_id),),
            ).fetchone()
        return bool(row and int(row["enabled"]))

    def get_group_todo_enabled(self, group_id: str) -> bool:
        """`is_group_todo_enabled` 的兼容别名。"""

        return self.is_group_todo_enabled(group_id)

    def set_group_todo_enabled(self, group_id: str, enabled: bool, now: int) -> None:
        """持久化群待办开关状态。"""

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO todo_group_configs (group_id, enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (str(group_id), 1 if enabled else 0, int(now)),
            )

    def get_mode(self, scope: str, group_id: str | None, user_id: str) -> str:
        """读取指定用户在当前范围内的提醒模式。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。

        Returns:
            提醒模式，未设置时返回 `concise`。
        """

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT mode
                FROM todo_reminder_modes
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return MODE_CONCISE
        mode = str(row["mode"])
        return mode if mode in {MODE_CONCISE, MODE_CATGIRL} else MODE_CONCISE

    def set_mode(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        mode: str,
        now: int,
    ) -> None:
        """保存指定用户在当前范围内的提醒模式。

        Args:
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。
            mode: 提醒模式，取值为 `concise` 或 `catgirl`。
            now: 更新时间的 Unix 秒级时间戳。
        """

        if mode not in {MODE_CONCISE, MODE_CATGIRL}:
            mode = MODE_CONCISE
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO todo_reminder_modes (user_id, mode, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (user_id, mode, int(now)),
            )

    def _transition(
        self,
        todo_id: int,
        from_status: str,
        to_status: str,
        user_id: str | None = None,
    ) -> TodoReminder | None:
        """在指定原状态匹配时切换待办状态。

        Args:
            todo_id: 待办内部主键 ID。
            from_status: 允许转换的原状态。
            to_status: 目标状态。

        Returns:
            更新后的待办记录；待办不存在或状态不匹配时返回 None。
        """

        with self._connect() as conn:
            user_sql = ""
            params: list[Any] = [to_status, int(todo_id)]
            if user_id is not None:
                user_sql = "AND user_id = ?"
                params.append(user_id)
            params.append(from_status)
            cursor = conn.execute(
                f"""
                UPDATE todo_reminders
                SET status = ?, revision = revision + 1
                WHERE id = ?
                  {user_sql}
                  AND status = ?
                """,
                params,
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM todo_reminders WHERE id = ?",
                (int(todo_id),),
            ).fetchone()
        return _row_to_todo(row) if row is not None else None

    def _transition_many(
        self,
        todo_ids: Sequence[int],
        user_id: str,
        from_status: str,
        to_status: str,
    ) -> list[TodoReminder] | None:
        """在一个事务内完成同状态的多条转换。"""

        target_ids = _unique_ints(todo_ids)
        if not target_ids:
            return []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            items = self._load_exact_targets(conn, target_ids, user_id)
            if items is None or any(item.status != from_status for item in items):
                return None
            placeholders = ",".join("?" for _ in target_ids)
            cursor = conn.execute(
                f"""
                UPDATE todo_reminders
                SET status = ?, revision = revision + 1
                WHERE user_id = ?
                  AND status = ?
                  AND id IN ({placeholders})
                """,
                [to_status, user_id, from_status, *target_ids],
            )
            if cursor.rowcount != len(target_ids):
                return None
            return self._load_exact_targets(conn, target_ids, user_id)

    @staticmethod
    def _load_exact_targets(
        conn: sqlite3.Connection,
        todo_ids: Sequence[int],
        user_id: str,
    ) -> list[TodoReminder] | None:
        """加载所有内部目标，缺失或越权时返回 None。"""

        target_ids = _unique_ints(todo_ids)
        if not target_ids:
            return []
        placeholders = ",".join("?" for _ in target_ids)
        rows = conn.execute(
            f"""
            SELECT * FROM todo_reminders
            WHERE user_id = ? AND id IN ({placeholders})
            """,
            [user_id, *target_ids],
        ).fetchall()
        if len(rows) != len(target_ids):
            return None
        by_id = {int(row["id"]): _row_to_todo(row) for row in rows}
        return [by_id[todo_id] for todo_id in target_ids]

    @staticmethod
    def _load_exact_history_targets(
        conn: sqlite3.Connection,
        history_ids: Sequence[str],
        user_id: str,
    ) -> list[TodoReminder] | None:
        """加载所有稳定历史目标，缺失或越权时返回 None。"""

        target_history_ids = _unique_history_ids(history_ids)
        if not target_history_ids:
            return []
        placeholders = ",".join("?" for _ in target_history_ids)
        rows = conn.execute(
            f"""
            SELECT * FROM todo_reminders
            WHERE user_id = ? AND history_id IN ({placeholders})
            """,
            [user_id, *target_history_ids],
        ).fetchall()
        if len(rows) != len(target_history_ids):
            return None
        by_history_id = {str(row["history_id"]): _row_to_todo(row) for row in rows}
        return [by_history_id[history_id] for history_id in target_history_ids]

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """为旧版本数据库补齐新增字段。

        Args:
            conn: 当前 SQLite 连接。
        """

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(todo_reminders)").fetchall()
        }
        if "todo_no" not in columns:
            conn.execute("ALTER TABLE todo_reminders ADD COLUMN todo_no INTEGER")
        if "history_id" not in columns:
            conn.execute("ALTER TABLE todo_reminders ADD COLUMN history_id TEXT")
        if "revision" not in columns:
            conn.execute(
                "ALTER TABLE todo_reminders ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )
        if "content" not in columns:
            conn.execute("ALTER TABLE todo_reminders ADD COLUMN content TEXT")
        if "due_at" not in columns:
            conn.execute("ALTER TABLE todo_reminders ADD COLUMN due_at INTEGER")
        if "reminder_text" not in columns:
            conn.execute(
                "ALTER TABLE todo_reminders ADD COLUMN reminder_text TEXT NOT NULL DEFAULT ''"
            )
        self._backfill_todo_no(conn)
        self._backfill_history_ids(conn)
        conn.execute("UPDATE todo_reminders SET revision = 1 WHERE revision IS NULL OR revision < 1")
        self._ensure_remind_at_nullable(conn)

    @staticmethod
    def _ensure_confirmation_table(conn: sqlite3.Connection) -> None:
        """兼容旧库：补建永久删除确认状态表。"""

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todo_operation_confirmations (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                target_snapshot TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )

    @staticmethod
    def _ensure_mode_table(conn: sqlite3.Connection) -> None:
        """把旧的按会话保存的提醒模式迁移为按用户保存。"""

        columns = {
            str(row["name"]): row
            for row in conn.execute("PRAGMA table_info(todo_reminder_modes)").fetchall()
        }
        if set(columns) == {"user_id", "mode", "updated_at"} and int(columns["user_id"]["pk"]) == 1:
            return

        rows = conn.execute(
            "SELECT user_id, mode, updated_at FROM todo_reminder_modes "
            "ORDER BY updated_at DESC, rowid DESC"
        ).fetchall()
        conn.execute(
            """
            CREATE TABLE todo_reminder_modes_new (
                user_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        seen: set[str] = set()
        for row in rows:
            user_id = str(row["user_id"])
            if user_id in seen:
                continue
            seen.add(user_id)
            conn.execute(
                "INSERT INTO todo_reminder_modes_new (user_id, mode, updated_at) VALUES (?, ?, ?)",
                (user_id, str(row["mode"]), int(row["updated_at"])),
            )
        conn.execute("DROP TABLE todo_reminder_modes")
        conn.execute("ALTER TABLE todo_reminder_modes_new RENAME TO todo_reminder_modes")

    @staticmethod
    def _ensure_remind_at_nullable(conn: sqlite3.Connection) -> None:
        """把旧表中的 remind_at NOT NULL 迁移为可空字段。

        Args:
            conn: 当前 SQLite 连接。
        """

        columns = {
            str(row["name"]): row
            for row in conn.execute("PRAGMA table_info(todo_reminders)").fetchall()
        }
        remind_at = columns.get("remind_at")
        if remind_at is None or int(remind_at["notnull"]) == 0:
            return

        conn.executescript(
            """
            CREATE TABLE todo_reminders_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                history_id TEXT NOT NULL UNIQUE,
                todo_no INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                scope TEXT NOT NULL,
                group_id TEXT,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT,
                raw_text TEXT NOT NULL,
                remind_at INTEGER,
                due_at INTEGER,
                reminder_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created_at INTEGER NOT NULL,
                reminded_at INTEGER,
                llm_json TEXT
            );

            INSERT INTO todo_reminders_new (
                id,
                history_id,
                todo_no,
                revision,
                scope,
                group_id,
                user_id,
                title,
                content,
                raw_text,
                remind_at,
                due_at,
                reminder_text,
                status,
                created_at,
                reminded_at,
                llm_json
            )
            SELECT
                id,
                history_id,
                todo_no,
                revision,
                scope,
                group_id,
                user_id,
                title,
                content,
                raw_text,
                remind_at,
                due_at,
                reminder_text,
                status,
                created_at,
                reminded_at,
                llm_json
            FROM todo_reminders;

            DROP TABLE todo_reminders;
            ALTER TABLE todo_reminders_new RENAME TO todo_reminders;
            """
        )

    def _ensure_indexes(self, conn: sqlite3.Connection) -> None:
        """创建查询索引和当前未完成待办的范围编号唯一索引。

        Args:
            conn: 当前 SQLite 连接。
        """

        conn.execute("DROP INDEX IF EXISTS idx_todo_scope_user_no")
        conn.execute("DROP INDEX IF EXISTS idx_todo_user_no")
        conn.execute("DROP INDEX IF EXISTS idx_todo_scope_status_remind")
        conn.execute("DROP INDEX IF EXISTS idx_todo_user_status_remind")
        conn.execute("DROP INDEX IF EXISTS idx_todo_history_id")
        self._repair_open_todo_numbers(conn)
        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_todo_user_no
            ON todo_reminders(user_id, todo_no)
            WHERE status = 'open';

            CREATE UNIQUE INDEX IF NOT EXISTS idx_todo_history_id
            ON todo_reminders(history_id);

            CREATE INDEX IF NOT EXISTS idx_todo_user_status_remind
            ON todo_reminders(user_id, status, remind_at, id);

            CREATE INDEX IF NOT EXISTS idx_todo_due
            ON todo_reminders(status, reminded_at, remind_at, id);

            CREATE INDEX IF NOT EXISTS idx_todo_confirmation_expiry
            ON todo_operation_confirmations(expires_at);
            """
        )

    @staticmethod
    def _repair_open_todo_numbers(conn: sqlite3.Connection) -> None:
        """修复旧库中会阻止唯一索引创建的未完成待办序号。

        旧版本数据库可能没有唯一索引，手工导入或历史 bug 也可能留下重复、
        空值或非正数序号。这里只改 `open` 记录，保留每个正数序号最早出现
        的记录，其余记录分配当前范围内最小可用正整数。

        Args:
            conn: 当前 SQLite 连接。
        """

        rows = conn.execute(
            """
            SELECT id, user_id, todo_no
            FROM todo_reminders
            WHERE status = ?
            ORDER BY user_id, created_at, id
            """,
            (STATUS_OPEN,),
        ).fetchall()
        grouped_rows: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            key = str(row["user_id"])
            grouped_rows.setdefault(key, []).append(row)

        for group_rows in grouped_rows.values():
            used_numbers: set[int] = set()
            repair_ids: list[int] = []
            for row in group_rows:
                todo_no = _positive_todo_no(row["todo_no"])
                if todo_no is not None and todo_no not in used_numbers:
                    used_numbers.add(todo_no)
                else:
                    repair_ids.append(int(row["id"]))

            for todo_id in repair_ids:
                todo_no = _first_available_todo_no(used_numbers)
                used_numbers.add(todo_no)
                conn.execute(
                    "UPDATE todo_reminders SET todo_no = ? WHERE id = ?",
                    (todo_no, todo_id),
                )

    @staticmethod
    def _used_open_todo_numbers(
        conn: sqlite3.Connection,
        user_id: str,
    ) -> set[int]:
        """查询同一范围内当前未完成待办已经占用的显示序号。

        Args:
            conn: 当前 SQLite 连接。
            scope: 待办来源范围，取值为 `group` 或 `private`。
            group_id: 群号；私聊待办传入 None。
            user_id: 创建人 QQ 号。

        Returns:
            状态为 `open` 的待办序号集合；软删除和完成的待办不会占用序号。
        """

        rows = conn.execute(
            """
            SELECT todo_no
            FROM todo_reminders
            WHERE user_id = ?
              AND status = ?
            """,
            (user_id, STATUS_OPEN),
        ).fetchall()
        return {int(row["todo_no"]) for row in rows}

    @staticmethod
    def _backfill_todo_no(conn: sqlite3.Connection) -> None:
        """给旧数据库中缺失 todo_no 的记录补齐范围内序号。

        Args:
            conn: 当前 SQLite 连接。
        """

        rows = conn.execute(
            """
            SELECT id, user_id
            FROM todo_reminders
            WHERE todo_no IS NULL
            ORDER BY user_id, created_at, id
            """
        ).fetchall()
        next_numbers: dict[str, int] = {}
        for row in rows:
            key = str(row["user_id"])
            if key not in next_numbers:
                max_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(todo_no), 0)
                    FROM todo_reminders
                    WHERE user_id = ?
                    """,
                    (key,),
                ).fetchone()
                next_numbers[key] = int(max_row[0] if max_row else 0) + 1

            conn.execute(
                "UPDATE todo_reminders SET todo_no = ? WHERE id = ?",
                (next_numbers[key], int(row["id"])),
            )
            next_numbers[key] += 1

    @staticmethod
    def _backfill_history_ids(conn: sqlite3.Connection) -> None:
        """为旧记录生成稳定且不会与新 ID 复用的历史标识。"""

        rows = conn.execute(
            """
            SELECT id
            FROM todo_reminders
            WHERE history_id IS NULL OR TRIM(history_id) = ''
            ORDER BY id
            """
        ).fetchall()
        for row in rows:
            # 迁移 ID 直接锚定旧主键；新记录使用随机 ID，二者永不复用。
            history_id = f"H-{int(row['id']):012d}"
            conn.execute(
                "UPDATE todo_reminders SET history_id = ? WHERE id = ?",
                (history_id, int(row["id"])),
            )

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """创建并托管 SQLite 连接的上下文管理器。

        正常退出时提交事务，发生异常时回滚事务，并最终关闭连接。

        Returns:
            可通过 with self._connect() as conn 使用的 SQLite 连接上下文管理器。

        Raises:
            Exception: 透传 with 代码块中抛出的异常。
        """

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()


def parse_pending_target_number(target: str) -> int | None:
    """解析用户输入的待办序号。

    Args:
        target: 用户输入，支持 `1`、`[3]` 或 `#3`。

    Returns:
        合法的正整数序号；格式不合法时返回 None。
    """

    normalized = target.strip()
    if normalized.startswith("["):
        if not normalized.endswith("]"):
            return None
        normalized = normalized[1:-1].strip()
    elif normalized.endswith("]"):
        return None
    elif normalized.startswith("#"):
        normalized = normalized[1:].strip()
    elif "#" in normalized or "[" in normalized:
        return None

    if not normalized.isdigit():
        return None
    number = int(normalized)
    return number if number > 0 else None


def validate_remind_at(
    remind_at: int | None,
    now: int,
    reject_past_reminder: bool,
) -> None:
    """唯一的提醒时间有效性校验入口。"""

    if reject_past_reminder and remind_at is not None and int(remind_at) <= int(now):
        raise ReminderTimeValidationError(int(remind_at), int(now))


def _new_history_id() -> str:
    """生成不可预测、不可复用的用户可见历史 ID。"""

    return f"H-{secrets.token_hex(12)}"


def _unique_history_ids(history_ids: Sequence[str]) -> list[str]:
    """规范化并保持用户指定的历史 ID 顺序。"""

    values: list[str] = []
    seen: set[str] = set()
    for value in history_ids:
        normalized = str(value).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(normalized)
    return values


def _unique_ints(values: Sequence[int]) -> list[int]:
    """去重并保留内部主键的调用顺序。"""

    return list(dict.fromkeys(int(value) for value in values))


def _row_to_todo(row: sqlite3.Row) -> TodoReminder:
    """把 SQLite 行对象转换为 TodoReminder。

    Args:
        row: SQLite 查询得到的一行。

    Returns:
        待办提醒记录。
    """

    return TodoReminder(
        id=int(row["id"]),
        history_id=str(row["history_id"]),
        todo_no=int(row["todo_no"]),
        revision=int(row["revision"]),
        scope=str(row["scope"]),
        group_id=row["group_id"],
        user_id=str(row["user_id"]),
        title=str(row["title"]),
        content=row["content"],
        raw_text=str(row["raw_text"]),
        remind_at=_optional_int(row["remind_at"]),
        due_at=_optional_int(row["due_at"]),
        reminder_text=str(row["reminder_text"] or ""),
        status=str(row["status"]),
        created_at=int(row["created_at"]),
        reminded_at=_optional_int(row["reminded_at"]),
        llm_json=row["llm_json"],
    )


def _optional_int(value: Any) -> int | None:
    """把可空数据库字段转换为可空整数。

    Args:
        value: SQLite 行中的字段值。

    Returns:
        None 或整数值。
    """

    return None if value is None else int(value)


def _positive_todo_no(value: Any) -> int | None:
    """把数据库里的待办序号转换为正整数，非法值返回 None。

    Args:
        value: SQLite 行中的 todo_no 字段值。

    Returns:
        合法正整数序号；空值、非数字或非正数返回 None。
    """

    try:
        parsed = _optional_int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed > 0 else None


def _first_available_todo_no(used_numbers: set[int]) -> int:
    """从当前未完成待办占用的序号中找出最小可用序号。

    Args:
        used_numbers: 当前范围内状态为 `open` 的待办序号集合。

    Returns:
        最小的正整数序号；例如已占用 `{1, 3}` 时返回 `2`。
    """

    todo_no = 1
    while todo_no in used_numbers:
        todo_no += 1
    return todo_no


def _group_key(group_id: str | None) -> str:
    """把可空群号转换为模式表里的稳定键。

    Args:
        group_id: 群号；私聊场景传入 None。

    Returns:
        用于联合主键的字符串，私聊场景固定为空字符串。
    """

    return "" if group_id is None else str(group_id)
