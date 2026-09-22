"""SQLite (aiosqlite): инициализация схемы и CRUD для tasks / synced_items."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    title            TEXT    NOT NULL,
    source_folder_id TEXT    NOT NULL,
    target_folder_id TEXT    NOT NULL,
    interval_sec     INTEGER NOT NULL DEFAULT 900,
    is_active        INTEGER NOT NULL DEFAULT 1,
    notify_on_update INTEGER NOT NULL DEFAULT 1,
    mirror_deletes   INTEGER NOT NULL DEFAULT 0,  -- удалять в назначении файлы, исчезнувшие из источника
    exclude_patterns TEXT    NOT NULL DEFAULT '', -- имена/маски («*.mp4») файлов, которые не копировать
    category         TEXT    NOT NULL DEFAULT '',
    template_enabled INTEGER NOT NULL DEFAULT 1,
    template_name    TEXT    NOT NULL DEFAULT '',
    template_mime    TEXT    NOT NULL DEFAULT '',
    template_data    BLOB,
    template_delay_sec INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    last_run_at      TEXT
);

CREATE TABLE IF NOT EXISTS synced_items (
    task_id        INTEGER NOT NULL,
    source_id      TEXT    NOT NULL,   -- ID файла/папки в источнике
    target_id      TEXT    NOT NULL,   -- ID созданной копии в назначении
    file_name      TEXT    NOT NULL,
    mime_type      TEXT    NOT NULL,
    modified_time  TEXT,               -- modifiedTime из Drive (UTC, ISO)
    size           TEXT    NOT NULL DEFAULT '',  -- размер для доп. сверки
    last_synced_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (task_id, source_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_user  ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_synced_task ON synced_items(task_id);

CREATE TABLE IF NOT EXISTS pending_templates (
    task_id          INTEGER NOT NULL,
    target_folder_id TEXT    NOT NULL,
    due_at           TEXT    NOT NULL,
    PRIMARY KEY (task_id, target_folder_id)
);
CREATE INDEX IF NOT EXISTS idx_pending_templates_due ON pending_templates(due_at);
"""


@dataclass(frozen=True)
class Task:
    """Связка «источник -> назначение»."""
    id: int
    user_id: int
    title: str
    source_folder_id: str
    target_folder_id: str
    interval_sec: int
    is_active: bool
    notify_on_update: bool
    mirror_deletes: bool
    exclude_patterns: str
    category: str
    template_enabled: bool
    template_name: str
    template_mime: str
    template_data: bytes | None
    template_delay_sec: int
    created_at: str
    last_run_at: str | None


@dataclass(frozen=True)
class SyncedItem:
    """Отражение одного элемента источника в назначении."""
    task_id: int
    source_id: str
    target_id: str
    file_name: str
    mime_type: str
    modified_time: str | None
    size: str
    last_synced_at: str


def _task_from_row(row: aiosqlite.Row) -> Task:
    return Task(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        title=str(row["title"]),
        source_folder_id=str(row["source_folder_id"]),
        target_folder_id=str(row["target_folder_id"]),
        interval_sec=int(row["interval_sec"]),
        is_active=bool(row["is_active"]),
        notify_on_update=bool(row["notify_on_update"]),
        mirror_deletes=bool(row["mirror_deletes"]),
        exclude_patterns=str(row["exclude_patterns"] or ""),
        category=str(row["category"] or ""),
        template_enabled=bool(row["template_enabled"]),
        template_name=str(row["template_name"] or ""),
        template_mime=str(row["template_mime"] or ""),
        template_data=row["template_data"],
        template_delay_sec=int(row["template_delay_sec"]),
        created_at=str(row["created_at"]),
        last_run_at=row["last_run_at"],
    )


def _item_from_row(row: aiosqlite.Row) -> SyncedItem:
    return SyncedItem(
        task_id=int(row["task_id"]),
        source_id=str(row["source_id"]),
        target_id=str(row["target_id"]),
        file_name=str(row["file_name"]),
        mime_type=str(row["mime_type"]),
        modified_time=row["modified_time"],
        size=str(row["size"] or ""),
        last_synced_at=str(row["last_synced_at"]),
    )


class Database:
    """Все запросы открывают короткое соединение — безопасно при малом RPS."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    async def init(self) -> None:
        """Создаёт директорию, схему, включает WAL-режим и накатывает миграции."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.executescript(SCHEMA)
            await self._migrate(conn)
            await conn.commit()
        log.info("База данных готова: %s", self._path)

    @staticmethod
    async def _migrate(conn: aiosqlite.Connection) -> None:
        """Добавляет новые столбцы в уже существующие базы (idempotent)."""
        cur = await conn.execute("PRAGMA table_info(tasks)")
        columns = {row[1] for row in await cur.fetchall()}
        migrations = {
            "mirror_deletes": "INTEGER NOT NULL DEFAULT 0",
            "category": "TEXT NOT NULL DEFAULT ''",
            "template_enabled": "INTEGER NOT NULL DEFAULT 1",
            "template_name": "TEXT NOT NULL DEFAULT ''",
            "template_mime": "TEXT NOT NULL DEFAULT ''",
            "template_data": "BLOB",
            "template_delay_sec": "INTEGER NOT NULL DEFAULT 0",
            "exclude_patterns": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in migrations.items():
            if name not in columns:
                await conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
                log.info("Миграция: добавлен столбец tasks.%s", name)

    # ------------------------------------------------------------------ tasks

    async def create_task(
        self,
        user_id: int,
        title: str,
        source_folder_id: str,
        target_folder_id: str,
        interval_sec: int,
        notify_on_update: bool,
        category: str = "",
        exclude_patterns: str = "",
        mirror_deletes: bool = False,
    ) -> int:
        async with aiosqlite.connect(self._path) as conn:
            cur = await conn.execute(
                """INSERT INTO tasks
                   (user_id, title, source_folder_id, target_folder_id,
                    interval_sec, is_active, notify_on_update, exclude_patterns, category,
                    mirror_deletes)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (user_id, title, source_folder_id, target_folder_id,
                 interval_sec, int(notify_on_update), exclude_patterns.strip()[:2000],
                 category.strip()[:64], int(mirror_deletes)),
            )
            await conn.commit()
            task_id = int(cur.lastrowid)  # type: ignore[arg-type]
            log.info("Создана задача #%s «%s» (user=%s)", task_id, title, user_id)
            return task_id

    async def get_task(self, task_id: int) -> Task | None:
        async with aiosqlite.connect(self._path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = await cur.fetchone()
            return _task_from_row(row) if row else None

    async def get_user_tasks(self, user_id: int) -> list[Task]:
        async with aiosqlite.connect(self._path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM tasks WHERE user_id = ? ORDER BY id", (user_id,)
            )
            rows = await cur.fetchall()
            return [_task_from_row(r) for r in rows]

    async def get_active_tasks(self) -> list[Task]:
        async with aiosqlite.connect(self._path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM tasks WHERE is_active = 1 ORDER BY id"
            )
            rows = await cur.fetchall()
            return [_task_from_row(r) for r in rows]

    async def set_task_active(self, task_id: int, is_active: bool) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "UPDATE tasks SET is_active = ? WHERE id = ?", (int(is_active), task_id)
            )
            await conn.commit()

    async def update_task_interval(self, task_id: int, interval_sec: int) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "UPDATE tasks SET interval_sec = ? WHERE id = ?", (interval_sec, task_id)
            )
            await conn.commit()

    async def update_task_notify(self, task_id: int, notify_on_update: bool) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "UPDATE tasks SET notify_on_update = ? WHERE id = ?",
                (int(notify_on_update), task_id),
            )
            await conn.commit()

    async def update_task_mirror_deletes(self, task_id: int, mirror_deletes: bool) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "UPDATE tasks SET mirror_deletes = ? WHERE id = ?",
                (int(mirror_deletes), task_id),
            )
            await conn.commit()

    async def update_task_exclude_patterns(self, task_id: int, patterns: str) -> None:
        """Сохраняет список исключений связки (пустая строка — очистить список)."""
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "UPDATE tasks SET exclude_patterns = ? WHERE id = ?",
                (patterns.strip()[:2000], task_id),
            )
            await conn.commit()

    async def update_task_source(self, task_id: int, source_folder_id: str) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "UPDATE tasks SET source_folder_id = ? WHERE id = ?", (source_folder_id, task_id)
            )
            await conn.commit()

    async def update_task_category(self, task_id: int, category: str) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "UPDATE tasks SET category = ? WHERE id = ?", (category.strip()[:64], task_id)
            )
            await conn.commit()

    async def update_task_template(
        self, task_id: int, enabled: bool, name: str = "", mime: str = "",
        data: bytes | None = None, delay_sec: int = 0,
    ) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                """UPDATE tasks SET template_enabled=?, template_name=?, template_mime=?,
                   template_data=?, template_delay_sec=? WHERE id=?""",
                (int(enabled), name[:255], mime[:255], data, max(0, delay_sec), task_id),
            )
            if not enabled:
                await conn.execute("DELETE FROM pending_templates WHERE task_id=?", (task_id,))
            await conn.commit()

    async def update_task_title(self, task_id: int, title: str) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "UPDATE tasks SET title = ? WHERE id = ?", (title, task_id)
            )
            await conn.commit()

    async def get_categories(self, user_id: int) -> list[str]:
        async with aiosqlite.connect(self._path) as conn:
            cur = await conn.execute(
                "SELECT DISTINCT category FROM tasks WHERE user_id=? AND category != '' ORDER BY category COLLATE NOCASE",
                (user_id,),
            )
            return [str(row[0]) for row in await cur.fetchall()]

    async def get_tasks_by_category(self, user_id: int, category: str) -> list[Task]:
        async with aiosqlite.connect(self._path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM tasks WHERE user_id=? AND category=? ORDER BY id", (user_id, category)
            )
            return [_task_from_row(row) for row in await cur.fetchall()]

    async def enqueue_template(self, task_id: int, target_folder_id: str, due_at: str) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO pending_templates(task_id,target_folder_id,due_at) VALUES(?,?,?)",
                (task_id, target_folder_id, due_at),
            )
            await conn.commit()

    async def get_due_templates(self, now_iso: str) -> list[tuple[Task, str]]:
        async with aiosqlite.connect(self._path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                """SELECT t.*, p.target_folder_id FROM pending_templates p
                   JOIN tasks t ON t.id=p.task_id
                   WHERE p.due_at <= ? AND t.template_enabled=1 AND t.template_data IS NOT NULL""", (now_iso,)
            )
            return [(_task_from_row(row), str(row["target_folder_id"])) for row in await cur.fetchall()]

    async def complete_template(self, task_id: int, target_folder_id: str) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute("DELETE FROM pending_templates WHERE task_id=? AND target_folder_id=?", (task_id, target_folder_id))
            await conn.commit()

    async def set_task_last_run(self, task_id: int, iso_time: str) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "UPDATE tasks SET last_run_at = ? WHERE id = ?", (iso_time, task_id)
            )
            await conn.commit()

    async def delete_task(self, task_id: int) -> None:
        """Удаляет задачу и её журнал синхронизации (файлы на Drive не трогаем)."""
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute("DELETE FROM synced_items WHERE task_id = ?", (task_id,))
            await conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            await conn.commit()
            log.info("Задача #%s удалена", task_id)

    # ----------------------------------------------------------- synced_items

    async def upsert_synced_item(
        self,
        task_id: int,
        source_id: str,
        target_id: str,
        file_name: str,
        mime_type: str,
        modified_time: str | None,
        size: str = "",
    ) -> None:
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                """INSERT INTO synced_items
                   (task_id, source_id, target_id, file_name,
                    mime_type, modified_time, size, last_synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(task_id, source_id) DO UPDATE SET
                       target_id      = excluded.target_id,
                       file_name      = excluded.file_name,
                       mime_type      = excluded.mime_type,
                       modified_time  = excluded.modified_time,
                       size           = excluded.size,
                       last_synced_at = excluded.last_synced_at""",
                (task_id, source_id, target_id, file_name,
                 mime_type, modified_time, size),
            )
            await conn.commit()

    async def upsert_synced_items(self, items: list[SyncedItem]) -> None:
        """Пакетно сохраняет журнал одного прохода синхронизации в одной транзакции."""
        if not items:
            return
        values = [
            (item.task_id, item.source_id, item.target_id, item.file_name,
             item.mime_type, item.modified_time, item.size)
            for item in items
        ]
        async with aiosqlite.connect(self._path) as conn:
            await conn.executemany(
                """INSERT INTO synced_items
                   (task_id, source_id, target_id, file_name,
                    mime_type, modified_time, size, last_synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(task_id, source_id) DO UPDATE SET
                       target_id=excluded.target_id, file_name=excluded.file_name,
                       mime_type=excluded.mime_type, modified_time=excluded.modified_time,
                       size=excluded.size, last_synced_at=excluded.last_synced_at""",
                values,
            )
            await conn.commit()

    async def get_synced_item(self, task_id: int, source_id: str) -> SyncedItem | None:
        async with aiosqlite.connect(self._path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM synced_items WHERE task_id = ? AND source_id = ?",
                (task_id, source_id),
            )
            row = await cur.fetchone()
            return _item_from_row(row) if row else None

    async def get_synced_items(self, task_id: int) -> list[SyncedItem]:
        async with aiosqlite.connect(self._path) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM synced_items WHERE task_id = ?", (task_id,)
            )
            rows = await cur.fetchall()
            return [_item_from_row(r) for r in rows]

    async def delete_synced_item(self, task_id: int, source_id: str) -> None:
        """Убирает элемент из журнала после зеркалирования удаления."""
        async with aiosqlite.connect(self._path) as conn:
            await conn.execute(
                "DELETE FROM synced_items WHERE task_id = ? AND source_id = ?",
                (task_id, source_id),
            )
            await conn.commit()

    async def count_synced_items(self, task_id: int) -> int:
        async with aiosqlite.connect(self._path) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM synced_items WHERE task_id = ?", (task_id,)
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0
