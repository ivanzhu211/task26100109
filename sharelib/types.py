"""数据类型定义：变更、游标、页面、内容项。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

STATUS_APPROVED = "approved"
STATUS_ARCHIVED = "archived"
VALID_STATUSES = (STATUS_APPROVED, STATUS_ARCHIVED)

PAGE_SIZE = 25


_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


def _check_int(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} 必须是整数，得到 {value!r}")
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise ValueError(f"{name} 超出 int64 范围: {value}")


def validate_fields(priority: int, status: str, window_start: int, window_end: int) -> None:
    _check_int("priority", priority)
    if not 1 <= priority <= 5:
        raise ValueError(f"priority 必须在 1..5，得到 {priority}")
    if status not in VALID_STATUSES:
        raise ValueError(f"status 必须是 {VALID_STATUSES} 之一，得到 {status!r}")
    _check_int("window_start", window_start)
    _check_int("window_end", window_end)
    if window_start > window_end:
        raise ValueError(f"window_start({window_start}) 不能大于 window_end({window_end})")


@dataclass(frozen=True)
class CreateContent:
    """新增内容变更。content_id 为 None 时由库自动分配。"""

    title: str
    body: str
    priority: int
    first_approved_at: int
    status: str
    window_start: int
    window_end: int
    content_id: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not isinstance(self.body, str):
            raise TypeError("title/body 必须是字符串")
        _check_int("first_approved_at", self.first_approved_at)
        if self.content_id is not None:
            _check_int("content_id", self.content_id)
        validate_fields(self.priority, self.status, self.window_start, self.window_end)


@dataclass(frozen=True)
class UpdateContent:
    """修改内容变更。为 None 的字段保持原值；first_approved_at 不可修改。"""

    content_id: int
    title: Optional[str] = None
    body: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None
    window_start: Optional[int] = None
    window_end: Optional[int] = None

    def __post_init__(self) -> None:
        _check_int("content_id", self.content_id)
        if self.title is not None and not isinstance(self.title, str):
            raise TypeError("title 必须是字符串")
        if self.body is not None and not isinstance(self.body, str):
            raise TypeError("body 必须是字符串")
        if self.priority is not None:
            validate_fields(self.priority, STATUS_APPROVED, 0, 0)
        if self.status is not None and self.status not in VALID_STATUSES:
            raise ValueError(f"status 必须是 {VALID_STATUSES} 之一，得到 {self.status!r}")
        if self.window_start is not None:
            _check_int("window_start", self.window_start)
        if self.window_end is not None:
            _check_int("window_end", self.window_end)


Change = Union[CreateContent, UpdateContent]


@dataclass(frozen=True)
class ContentItem:
    content_id: int
    title: str
    body: str
    priority: int
    first_approved_at: int
    status: str
    window_start: int
    window_end: int


@dataclass(frozen=True)
class Cursor:
    """浏览游标。不可变值对象；重复使用返回相同一页。"""

    browse_id: int
    last_priority: Optional[int] = None
    last_first_approved_at: Optional[int] = None
    last_content_id: Optional[int] = None


@dataclass(frozen=True)
class Page:
    items: tuple[ContentItem, ...]
    next_cursor: Optional[Cursor]
