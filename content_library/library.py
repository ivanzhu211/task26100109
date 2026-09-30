"""员工分享内容库：基于 SQLite 的稳定分页浏览。"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

from .types import (
    PAGE_SIZE,
    STATUS_APPROVED,
    STATUS_ARCHIVED,
    ContentChange,
    ContentItem,
    Page,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    item_id INTEGER PRIMARY KEY,
    first_approved_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    item_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    PRIMARY KEY (item_id, version)
);
CREATE TABLE IF NOT EXISTS current (
    item_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    sort_pri INTEGER NOT NULL,
    sort_fa INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_current_order
    ON current (sort_pri, sort_fa, item_id);
"""

_VALID_STATUS = {STATUS_APPROVED, STATUS_ARCHIVED}


class ContentLibrary:
    """内容库主入口，由上层直接实例化调用。"""

    def __init__(self, db_path: str) -> None:
        self._db = sqlite3.connect(db_path)
        self._db.execute("PRAGMA journal_mode=DELETE")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        row = self._db.execute(
            "SELECT value FROM meta WHERE key='version'"
        ).fetchone()
        self._version: int = row[0] if row else 0
        self._next_browse_id = 0
        self._browses: dict[int, tuple[int, int]] = {}

    @property
    def version(self) -> int:
        return self._version

    def submit(self, changes: Iterable[ContentChange]) -> int:
        """提交一批变更，整批原子生效，返回新的版本号。"""
        merged: dict[int, ContentChange] = {}
        for change in changes:
            merged[change.item_id] = change
        if not merged:
            return self._version

        version = self._version + 1
        with self._db:
            for change in merged.values():
                self._apply_change(change, version)
            self._db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)",
                (version,),
            )
        self._version = version
        return version

    def _apply_change(self, change: ContentChange, version: int) -> None:
        row = self._db.execute(
            "SELECT first_approved_at FROM items WHERE item_id=?",
            (change.item_id,),
        ).fetchone()
        if row is None:
            self._insert_new(change, version)
        else:
            self._update_existing(change, version, row[0])

    def _insert_new(self, change: ContentChange, version: int) -> None:
        fields = (
            change.title,
            change.body,
            change.priority,
            change.status,
            change.window_start,
            change.window_end,
            change.first_approved_at,
        )
        if any(f is None for f in fields):
            raise ValueError(
                f"新建内容 {change.item_id} 必须提供全部业务字段"
            )
        self._validate(change.priority, change.status)
        self._db.execute(
            "INSERT INTO items (item_id, first_approved_at) VALUES (?, ?)",
            (change.item_id, change.first_approved_at),
        )
        self._write_state(
            change.item_id,
            version,
            change.title,
            change.body,
            change.priority,
            change.status,
            change.window_start,
            change.window_end,
            change.first_approved_at,
        )

    def _update_existing(
        self, change: ContentChange, version: int, first_approved_at: int
    ) -> None:
        if (
            change.first_approved_at is not None
            and change.first_approved_at != first_approved_at
        ):
            raise ValueError(
                f"内容 {change.item_id} 的首次批准时刻不可修改"
            )
        cur = self._db.execute(
            "SELECT title, body, priority, status, window_start, window_end"
            " FROM current WHERE item_id=?",
            (change.item_id,),
        ).fetchone()
        title = change.title if change.title is not None else cur[0]
        body = change.body if change.body is not None else cur[1]
        priority = change.priority if change.priority is not None else cur[2]
        status = change.status if change.status is not None else cur[3]
        window_start = (
            change.window_start if change.window_start is not None else cur[4]
        )
        window_end = (
            change.window_end if change.window_end is not None else cur[5]
        )
        self._validate(priority, status)
        self._write_state(
            change.item_id,
            version,
            title,
            body,
            priority,
            status,
            window_start,
            window_end,
            first_approved_at,
        )

    @staticmethod
    def _validate(priority: int, status: str) -> None:
        if not 1 <= priority <= 5:
            raise ValueError(f"优先级必须在 1 至 5 之间: {priority}")
        if status not in _VALID_STATUS:
            raise ValueError(f"非法状态: {status}")

    def _write_state(
        self,
        item_id: int,
        version: int,
        title: str,
        body: str,
        priority: int,
        status: str,
        window_start: int,
        window_end: int,
        first_approved_at: int,
    ) -> None:
        self._db.execute(
            "INSERT INTO revisions"
            " (item_id, version, title, body, priority, status,"
            "  window_start, window_end)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                version,
                title,
                body,
                priority,
                status,
                window_start,
                window_end,
            ),
        )
        self._db.execute(
            "INSERT OR REPLACE INTO current"
            " (item_id, title, body, priority, status,"
            "  window_start, window_end, sort_pri, sort_fa)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                title,
                body,
                priority,
                status,
                window_start,
                window_end,
                -priority,
                -first_approved_at,
            ),
        )

    def start_browse(self, t_ms: int) -> Page:
        """以时刻 T 开始一次新浏览，返回第一页。"""
        browse_id = self._next_browse_id
        self._next_browse_id += 1
        self._browses[browse_id] = (self._version, t_ms)
        return self._fetch_page(browse_id, None)

    def continue_browse(self, cursor: str) -> Page:
        """用上一页返回的游标继续翻页。"""
        browse_id, keyset = self._parse_cursor(cursor)
        if browse_id not in self._browses:
            raise ValueError(f"未知游标: {cursor}")
        return self._fetch_page(browse_id, keyset)

    @staticmethod
    def _parse_cursor(cursor: str) -> tuple[int, tuple[int, int, int]]:
        parts = cursor.split(":")
        if len(parts) != 4:
            raise ValueError(f"非法游标: {cursor}")
        browse_id, sort_pri, sort_fa, item_id = (int(p) for p in parts)
        return browse_id, (sort_pri, sort_fa, item_id)

    def _make_cursor(
        self, browse_id: int, item: ContentItem
    ) -> str:
        return (
            f"{browse_id}:{-item.priority}:"
            f"{-item.first_approved_at}:{item.item_id}"
        )

    def _fetch_page(
        self,
        browse_id: int,
        keyset: Optional[tuple[int, int, int]],
    ) -> Page:
        version, t_ms = self._browses[browse_id]
        if version == self._version:
            items = self._fetch_current(t_ms, keyset)
        else:
            items = self._fetch_history(version, t_ms, keyset)
        cursor = None
        if len(items) == PAGE_SIZE:
            cursor = self._make_cursor(browse_id, items[-1])
        return Page(items=tuple(items), cursor=cursor)

    def _fetch_current(
        self, t_ms: int, keyset: Optional[tuple[int, int, int]]
    ) -> list[ContentItem]:
        sql = (
            "SELECT item_id, title, body, priority, status,"
            " window_start, window_end, sort_fa FROM current"
            " WHERE status=? AND window_start<=? AND window_end>?"
        )
        params: list = [STATUS_APPROVED, t_ms, t_ms]
        if keyset is not None:
            sql += " AND (sort_pri, sort_fa, item_id) > (?, ?, ?)"
            params.extend(keyset)
        sql += " ORDER BY sort_pri, sort_fa, item_id LIMIT ?"
        params.append(PAGE_SIZE)
        rows = self._db.execute(sql, params).fetchall()
        return [
            ContentItem(
                item_id=r[0],
                title=r[1],
                body=r[2],
                priority=r[3],
                status=r[4],
                window_start=r[5],
                window_end=r[6],
                first_approved_at=-r[7],
            )
            for r in rows
        ]

    def _fetch_history(
        self,
        version: int,
        t_ms: int,
        keyset: Optional[tuple[int, int, int]],
    ) -> list[ContentItem]:
        rows = self._db.execute(
            "SELECT r.item_id, r.title, r.body, r.priority, r.status,"
            " r.window_start, r.window_end, i.first_approved_at"
            " FROM revisions r"
            " JOIN (SELECT item_id, MAX(version) AS mv FROM revisions"
            "       WHERE version<=? GROUP BY item_id) m"
            "   ON r.item_id=m.item_id AND r.version=m.mv"
            " JOIN items i ON i.item_id=r.item_id"
            " WHERE r.status=? AND r.window_start<=? AND r.window_end>?",
            (version, STATUS_APPROVED, t_ms, t_ms),
        ).fetchall()
        items = [
            ContentItem(
                item_id=r[0],
                title=r[1],
                body=r[2],
                priority=r[3],
                status=r[4],
                window_start=r[5],
                window_end=r[6],
                first_approved_at=r[7],
            )
            for r in rows
        ]
        items.sort(
            key=lambda it: (-it.priority, -it.first_approved_at, it.item_id)
        )
        if keyset is not None:
            sort_pri, sort_fa, item_id = keyset
            items = [
                it
                for it in items
                if (-it.priority, -it.first_approved_at, it.item_id)
                > (sort_pri, sort_fa, item_id)
            ]
        return items[:PAGE_SIZE]

    def close(self) -> None:
        self._db.close()
