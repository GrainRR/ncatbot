"""待办提醒插件的 SQLite 存储层。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TODO_DB_FILENAME = "todos.sqlite"
STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_DELETED = "deleted"
MODE_CONCISE = "concise"
MODE_CATGIRL = "catgirl"


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
    todo_no: int
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
                    todo_no INTEGER NOT NULL,
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
                    scope TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (scope, group_id, user_id)
                );
                """
            )
            self._ensure_columns(conn)
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
                WHERE scope = ?
                  AND COALESCE(group_id, '') = COALESCE(?, '')
                  AND user_id = ?
                  AND status = ?
                """,
                (scope, group_id, user_id, STATUS_OPEN),
            ).fetchone()
        return int(row[0] if row else 0)

    def create_many(
        self,
        scope: str,
        group_id: str | None,
        user_id: str,
        drafts: list[TodoReminderDraft],
        now: int,
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
            used_todo_numbers = self._used_open_todo_numbers(conn, scope, group_id, user_id)
            todo_ids: list[int] = []
            for draft in drafts:
                todo_no = _first_available_todo_no(used_todo_numbers)
                cursor = conn.execute(
                    """
                    INSERT INTO todo_reminders (
                        todo_no,
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
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
                f"SELECT * FROM todo_reminders WHERE id IN ({placeholders}) ORDER BY id ASC",
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
                WHERE scope = ?
                  AND COALESCE(group_id, '') = COALESCE(?, '')
                  AND user_id = ?
                  AND status = ?
                ORDER BY remind_at IS NULL ASC, remind_at ASC, id ASC
                LIMIT ?
                """,
                (scope, group_id, user_id, STATUS_OPEN, int(limit)),
            ).fetchall()
        return [_row_to_todo(row) for row in rows]

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
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM todo_reminders
                WHERE scope = ?
                  AND COALESCE(group_id, '') = COALESCE(?, '')
                  AND user_id = ?
                  AND todo_no = ?
                  AND status = ?
                LIMIT 1
                """,
                (scope, group_id, user_id, int(todo_no), STATUS_OPEN),
            ).fetchone()
        return _row_to_todo(row) if row is not None else None

    def complete(self, todo_id: int) -> TodoReminder | None:
        """把待办标记为已完成。

        Args:
            todo_id: 待办内部主键 ID。

        Returns:
            更新后的待办记录；待办不存在或状态不是 `open` 时返回 None。
        """

        return self._transition(todo_id, STATUS_OPEN, STATUS_DONE)

    def cancel(self, todo_id: int) -> TodoReminder | None:
        """把待办标记为已删除。

        Args:
            todo_id: 待办内部主键 ID。

        Returns:
            更新后的待办记录；待办不存在或状态不是 `open` 时返回 None。
        """

        return self._transition(todo_id, STATUS_OPEN, STATUS_DELETED)

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
                ORDER BY remind_at ASC, id ASC
                LIMIT ?
                """,
                (STATUS_OPEN, int(now), int(limit)),
            ).fetchall()
        return [_row_to_todo(row) for row in rows]

    def mark_reminded(self, todo_id: int, now: int) -> None:
        """标记待办已经提醒过，并自动软删除。

        Args:
            todo_id: 待办内部主键 ID。
            now: 提醒成功时的 Unix 秒级时间戳。
        """

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE todo_reminders
                SET reminded_at = ?,
                    status = ?
                WHERE id = ?
                  AND reminded_at IS NULL
                  AND status = ?
                """,
                (int(now), STATUS_DELETED, int(todo_id), STATUS_OPEN),
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
                WHERE scope = ?
                  AND group_id = ?
                  AND user_id = ?
                """,
                (scope, _group_key(group_id), user_id),
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
                INSERT INTO todo_reminder_modes (
                    scope,
                    group_id,
                    user_id,
                    mode,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope, group_id, user_id) DO UPDATE SET
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (scope, _group_key(group_id), user_id, mode, int(now)),
            )

    def _transition(
        self,
        todo_id: int,
        from_status: str,
        to_status: str,
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
            cursor = conn.execute(
                """
                UPDATE todo_reminders
                SET status = ?
                WHERE id = ?
                  AND status = ?
                """,
                (to_status, int(todo_id), from_status),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM todo_reminders WHERE id = ?",
                (int(todo_id),),
            ).fetchone()
        return _row_to_todo(row) if row is not None else None

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
        if "content" not in columns:
            conn.execute("ALTER TABLE todo_reminders ADD COLUMN content TEXT")
        if "due_at" not in columns:
            conn.execute("ALTER TABLE todo_reminders ADD COLUMN due_at INTEGER")
        if "reminder_text" not in columns:
            conn.execute(
                "ALTER TABLE todo_reminders ADD COLUMN reminder_text TEXT NOT NULL DEFAULT ''"
            )
        self._backfill_todo_no(conn)
        self._ensure_remind_at_nullable(conn)

    def _ensure_remind_at_nullable(self, conn: sqlite3.Connection) -> None:
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
                todo_no INTEGER NOT NULL,
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
                todo_no,
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
                todo_no,
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
        self._repair_open_todo_numbers(conn)
        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_todo_scope_user_no
            ON todo_reminders(scope, IFNULL(group_id, ''), user_id, todo_no)
            WHERE status = 'open';

            CREATE INDEX IF NOT EXISTS idx_todo_scope_status_remind
            ON todo_reminders(scope, group_id, user_id, status, remind_at, id);

            CREATE INDEX IF NOT EXISTS idx_todo_due
            ON todo_reminders(status, reminded_at, remind_at, id);
            """
        )

    def _repair_open_todo_numbers(self, conn: sqlite3.Connection) -> None:
        """修复旧库中会阻止唯一索引创建的未完成待办序号。

        旧版本数据库可能没有唯一索引，手工导入或历史 bug 也可能留下重复、
        空值或非正数序号。这里只改 `open` 记录，保留每个正数序号最早出现
        的记录，其余记录分配当前范围内最小可用正整数。

        Args:
            conn: 当前 SQLite 连接。
        """

        rows = conn.execute(
            """
            SELECT id, scope, group_id, user_id, todo_no
            FROM todo_reminders
            WHERE status = ?
            ORDER BY scope ASC, COALESCE(group_id, '') ASC, user_id ASC, created_at ASC, id ASC
            """,
            (STATUS_OPEN,),
        ).fetchall()
        grouped_rows: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (str(row["scope"]), _group_key(row["group_id"]), str(row["user_id"]))
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

    def _used_open_todo_numbers(
        self,
        conn: sqlite3.Connection,
        scope: str,
        group_id: str | None,
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
            WHERE scope = ?
              AND COALESCE(group_id, '') = COALESCE(?, '')
              AND user_id = ?
              AND status = ?
            """,
            (scope, group_id, user_id, STATUS_OPEN),
        ).fetchall()
        return {int(row["todo_no"]) for row in rows}

    def _backfill_todo_no(self, conn: sqlite3.Connection) -> None:
        """给旧数据库中缺失 todo_no 的记录补齐范围内序号。

        Args:
            conn: 当前 SQLite 连接。
        """

        rows = conn.execute(
            """
            SELECT id, scope, group_id, user_id
            FROM todo_reminders
            WHERE todo_no IS NULL
            ORDER BY scope ASC, COALESCE(group_id, '') ASC, user_id ASC, created_at ASC, id ASC
            """
        ).fetchall()
        next_numbers: dict[tuple[str, str, str], int] = {}
        for row in rows:
            key = (str(row["scope"]), _group_key(row["group_id"]), str(row["user_id"]))
            if key not in next_numbers:
                max_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(todo_no), 0)
                    FROM todo_reminders
                    WHERE scope = ?
                      AND COALESCE(group_id, '') = ?
                      AND user_id = ?
                    """,
                    key,
                ).fetchone()
                next_numbers[key] = int(max_row[0] if max_row else 0) + 1

            conn.execute(
                "UPDATE todo_reminders SET todo_no = ? WHERE id = ?",
                (next_numbers[key], int(row["id"])),
            )
            next_numbers[key] += 1

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


def _row_to_todo(row: sqlite3.Row) -> TodoReminder:
    """把 SQLite 行对象转换为 TodoReminder。

    Args:
        row: SQLite 查询得到的一行。

    Returns:
        待办提醒记录。
    """

    return TodoReminder(
        id=int(row["id"]),
        todo_no=int(row["todo_no"]),
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
    """把可空数据库字段转换为可空整数。"""

    return None if value is None else int(value)


def _positive_todo_no(value: Any) -> int | None:
    """把数据库里的待办序号转换为正整数，非法值返回 None。"""

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
    """把可空群号转换为模式表里的稳定键。"""

    return "" if group_id is None else str(group_id)
