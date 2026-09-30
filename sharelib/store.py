"""基于 SQLite 的员工分享内容库：提交变更、开始浏览、继续浏览。

设计要点：
- content_current 保存每条内容的最新已提交版本（每内容一行）。
- content_history 保存全部历史版本（含最新版），按 (content_id, version) 递增。
- 每次 commit 分配单调递增的 commit_seq，同批变更共享同一序号，整体原子生效。
- 浏览只记录 (browse_id, commit_seq, t_ms)，翻页时按该序号做"截至彼时"查询，
  因此不为每次浏览物化目录，5000 个浏览的新增持久化数据仅为浏览表小行。
- 排序键 (priority 降, first_approved_at 降, content_id 升) 编码为单列字符串
  sort_key 并建覆盖索引，翻页即"sort_key > 游标"的单列范围定位，
  每页只触碰约一页的索引行。
- 若浏览开始后又有提交（慢路径），结果 = 未变内容的当前版本（流 A，走索引）
  与变更内容截至浏览时的旧版本（流 B，数量受此后提交数限制）按同一排序键归并。
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_right
from typing import Iterable, Optional

from .types import (
    PAGE_SIZE,
    STATUS_APPROVED,
    Change,
    ContentItem,
    CreateContent,
    Cursor,
    Page,
    UpdateContent,
    validate_fields,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
    seq INTEGER PRIMARY KEY,
    change_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS content_current (
    content_id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL,
    commit_seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    priority INTEGER NOT NULL,
    first_approved_at INTEGER NOT NULL,
    status TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    sort_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_current_sort ON content_current (
    sort_key, commit_seq, status, window_start, window_end,
    title, body, priority, first_approved_at
);
CREATE INDEX IF NOT EXISTS idx_current_commit ON content_current (commit_seq);
CREATE TABLE IF NOT EXISTS content_history (
    content_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    commit_seq INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    priority INTEGER NOT NULL,
    first_approved_at INTEGER NOT NULL,
    status TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    PRIMARY KEY (content_id, version)
);
CREATE TABLE IF NOT EXISTS browses (
    browse_id INTEGER PRIMARY KEY,
    commit_seq INTEGER NOT NULL,
    t_ms INTEGER NOT NULL
);
"""

_ITEM_COLS = ("content_id", "title", "body", "priority", "first_approved_at",
              "status", "window_start", "window_end")

_VISIBLE = "status = ? AND window_start <= ? AND window_end > ?"

_INT64 = 1 << 63


def encode_sort_key(priority: int, first_approved_at: int, content_id: int) -> str:
    """把 (priority 降, first_approved_at 降, content_id 升) 编码为可字典序比较的单列键。"""
    return (f"{5 - priority}"
            f"{_INT64 - 1 - first_approved_at:020d}"
            f"{content_id + _INT64:020d}")


def _tuple_key(item: ContentItem) -> tuple[int, int, int]:
    return (-item.priority, -item.first_approved_at, item.content_id)


def _row_to_item(row: tuple) -> ContentItem:
    return ContentItem(*row)


class ContentLibrary:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute("SELECT MAX(seq) FROM commits").fetchone()
        self._latest_seq: int = row[0] or 0
        row = self._conn.execute("SELECT MAX(content_id) FROM content_current").fetchone()
        self._max_content_id: int = row[0] or 0
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._conn.commit()
            self._conn.close()
            self._closed = True

    def __enter__(self) -> "ContentLibrary":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 提交变更 ----

    def commit(self, changes: Iterable[Change]) -> int:
        """原子提交一批变更，返回提交序号。空批次报错。"""
        batch = list(changes)
        if not batch:
            raise ValueError("提交批次不能为空")
        conn = self._conn
        seq = self._latest_seq + 1
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO commits (seq, change_count) VALUES (?, ?)",
                (seq, len(batch)),
            )
            for change in batch:
                if isinstance(change, CreateContent):
                    self._apply_create(seq, change)
                elif isinstance(change, UpdateContent):
                    self._apply_update(seq, change)
                else:
                    raise TypeError(f"未知变更类型: {type(change)!r}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self._latest_seq = seq
        return seq

    def _apply_create(self, seq: int, change: CreateContent) -> None:
        content_id = change.content_id
        if content_id is None:
            content_id = self._max_content_id + 1
        if content_id <= self._max_content_id or self._exists(content_id):
            raise ValueError(f"content_id {content_id} 已存在")
        sort_key = encode_sort_key(change.priority, change.first_approved_at,
                                   content_id)
        row = (
            content_id,
            1,
            seq,
            change.title,
            change.body,
            change.priority,
            change.first_approved_at,
            change.status,
            change.window_start,
            change.window_end,
            sort_key,
        )
        self._conn.execute(
            "INSERT INTO content_current VALUES (?,?,?,?,?,?,?,?,?,?,?)", row
        )
        self._conn.execute(
            "INSERT INTO content_history VALUES (?,?,?,?,?,?,?,?,?,?)", row[:10]
        )
        if content_id > self._max_content_id:
            self._max_content_id = content_id

    def _apply_update(self, seq: int, change: UpdateContent) -> None:
        row = self._conn.execute(
            "SELECT version, title, body, priority, first_approved_at, status,"
            " window_start, window_end FROM content_current WHERE content_id = ?",
            (change.content_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"content_id {change.content_id} 不存在")
        version, title, body, priority, first_approved_at, status, w_start, w_end = row
        if change.title is not None:
            title = change.title
        if change.body is not None:
            body = change.body
        if change.priority is not None:
            priority = change.priority
        if change.status is not None:
            status = change.status
        if change.window_start is not None:
            w_start = change.window_start
        if change.window_end is not None:
            w_end = change.window_end
        validate_fields(priority, status, w_start, w_end)
        new_version = version + 1
        sort_key = encode_sort_key(priority, first_approved_at, change.content_id)
        self._conn.execute(
            "UPDATE content_current SET version=?, commit_seq=?, title=?, body=?,"
            " priority=?, status=?, window_start=?, window_end=?, sort_key=?"
            " WHERE content_id=?",
            (new_version, seq, title, body, priority, status, w_start, w_end,
             sort_key, change.content_id),
        )
        self._conn.execute(
            "INSERT INTO content_history VALUES (?,?,?,?,?,?,?,?,?,?)",
            (change.content_id, new_version, seq, title, body, priority,
             first_approved_at, status, w_start, w_end),
        )

    def _exists(self, content_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM content_current WHERE content_id = ?", (content_id,)
        ).fetchone()
        return row is not None

    # ---- 浏览 ----

    def start_browse(self, t_ms: int) -> Cursor:
        """以时刻 T 开始一次浏览，固定内容集合、顺序与字段值。"""
        if not isinstance(t_ms, int) or isinstance(t_ms, bool):
            raise TypeError("t_ms 必须是整数（UTC 毫秒）")
        cur = self._conn.execute(
            "INSERT INTO browses (commit_seq, t_ms) VALUES (?, ?)",
            (self._latest_seq, t_ms),
        )
        self._conn.commit()
        return Cursor(browse_id=cur.lastrowid)

    def continue_browse(self, cursor: Cursor) -> Page:
        """取游标之后的一页；next_cursor 为 None 表示已读完。"""
        row = self._conn.execute(
            "SELECT commit_seq, t_ms FROM browses WHERE browse_id = ?",
            (cursor.browse_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"browse_id {cursor.browse_id} 不存在")
        seq, t_ms = row
        if seq == self._latest_seq:
            items = self._fetch_current(seq, t_ms, cursor)
        else:
            items = self._fetch_merged(seq, t_ms, cursor)
        has_more = len(items) > PAGE_SIZE
        items = items[:PAGE_SIZE]
        next_cursor: Optional[Cursor] = None
        if has_more:
            last = items[-1]
            next_cursor = Cursor(
                browse_id=cursor.browse_id,
                last_priority=last.priority,
                last_first_approved_at=last.first_approved_at,
                last_content_id=last.content_id,
            )
        return Page(items=tuple(items), next_cursor=next_cursor)

    def _fetch_current(self, seq: int, t_ms: int, cursor: Cursor) -> list[ContentItem]:
        """流 A（也是快路径）：未变内容的当前版本，沿 sort_key 覆盖索引定位。"""
        sql = (
            f"SELECT {', '.join(_ITEM_COLS)} FROM content_current"
            f" WHERE commit_seq <= ? AND {_VISIBLE}"
        )
        args: list = [seq, STATUS_APPROVED, t_ms, t_ms]
        if cursor.last_content_id is not None:
            sql += " AND sort_key > ?"
            args.append(encode_sort_key(cursor.last_priority,
                                        cursor.last_first_approved_at,
                                        cursor.last_content_id))
        sql += " ORDER BY sort_key ASC LIMIT ?"
        args.append(PAGE_SIZE + 1)
        rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_item(r) for r in rows]

    def _fetch_merged(self, seq: int, t_ms: int, cursor: Cursor) -> list[ContentItem]:
        """慢路径：流 A（未变内容的当前版本）与流 B（变更内容的旧版本）归并。"""
        stream_a = self._fetch_current(seq, t_ms, cursor)
        stream_b = self._fetch_overlay(seq, t_ms, cursor)
        merged = sorted((*stream_a, *stream_b), key=_tuple_key)
        return merged[: PAGE_SIZE + 1]

    def _fetch_overlay(self, seq: int, t_ms: int, cursor: Cursor) -> list[ContentItem]:
        """流 B：浏览之后有变更的内容，取截至浏览 seq 的最新版本。"""
        sql = f"""
            SELECT h.content_id, h.title, h.body, h.priority, h.first_approved_at,
                   h.status, h.window_start, h.window_end
            FROM content_current c
            JOIN content_history h ON h.content_id = c.content_id
            WHERE c.commit_seq > ?
              AND h.version = (
                    SELECT MAX(h2.version) FROM content_history h2
                    WHERE h2.content_id = c.content_id AND h2.commit_seq <= ?)
        """
        rows = self._conn.execute(sql, (seq, seq)).fetchall()
        items = [
            _row_to_item(r)
            for r in rows
            if r[5] == STATUS_APPROVED and r[6] <= t_ms < r[7]
        ]
        items.sort(key=_tuple_key)
        if cursor.last_content_id is not None:
            bound = (-cursor.last_priority, -cursor.last_first_approved_at,
                     cursor.last_content_id)
            keys = [_tuple_key(it) for it in items]
            items = items[bisect_right(keys, bound):]
        return items[: PAGE_SIZE + 1]
