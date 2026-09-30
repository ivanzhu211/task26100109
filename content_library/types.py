"""公共类型定义：变更、内容条目、分页结果。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

PAGE_SIZE = 25

STATUS_APPROVED = "approved"
STATUS_ARCHIVED = "archived"


@dataclass(frozen=True)
class ContentChange:
    """一条内容变更。

    item_id 不存在时表示新建，此时所有业务字段都必须提供；
    item_id 已存在时表示修改，只需提供要变更的字段。
    first_approved_at 仅在新建时提供，之后不可修改。
    所有时刻均为 UTC 毫秒。
    """

    item_id: int
    title: Optional[str] = None
    body: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    window_start: Optional[int] = None
    window_end: Optional[int] = None
    first_approved_at: Optional[int] = None


@dataclass(frozen=True)
class ContentItem:
    """内容条目某一版本的完整字段快照。"""

    item_id: int
    title: str
    body: str
    priority: int
    status: str
    window_start: int
    window_end: int
    first_approved_at: int


@dataclass(frozen=True)
class Page:
    """一页结果。cursor 为 None 表示浏览结束。"""

    items: tuple[ContentItem, ...]
    cursor: Optional[str]
