"""正确性测试：快照稳定性、排序、窗口边界、游标语义。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from content_library import ContentChange, ContentLibrary

T0 = 1_000_000


def make_change(item_id, priority=3, window=(0, 10_000_000), **kw):
    args = dict(
        item_id=item_id,
        title=f"title-{item_id}",
        body=f"body-{item_id}",
        priority=priority,
        status="approved",
        window_start=window[0],
        window_end=window[1],
        first_approved_at=T0 + item_id,
    )
    args.update(kw)
    return ContentChange(**args)


class Model:
    """测试侧独立维护的内容模型，用于核对库的输出。"""

    def __init__(self):
        self.items = {}

    def apply(self, change):
        cur = self.items.get(change.item_id)
        if cur is None:
            cur = {}
            self.items[change.item_id] = cur
        for field in (
            "title", "body", "priority", "status",
            "window_start", "window_end", "first_approved_at",
        ):
            value = getattr(change, field)
            if value is not None:
                cur[field] = value

    def catalog(self, t_ms):
        visible = [
            dict(item_id=item_id, **fields)
            for item_id, fields in self.items.items()
            if fields["status"] == "approved"
            and fields["window_start"] <= t_ms < fields["window_end"]
        ]
        visible.sort(
            key=lambda it: (
                -it["priority"], -it["first_approved_at"], it["item_id"]
            )
        )
        return visible


def drain(lib, page):
    """读完一个浏览的所有页，返回完整条目列表。"""
    items = list(page.items)
    while page.cursor is not None:
        page = lib.continue_browse(page.cursor)
        items.extend(page.items)
    return items


def as_dicts(items):
    return [dict(it.__dict__) for it in items]


class SnapshotStabilityTest(unittest.TestCase):
    def setUp(self):
        self.lib = ContentLibrary(":memory:")
        self.model = Model()

    def tearDown(self):
        self.lib.close()

    def submit(self, changes):
        for change in changes:
            self.model.apply(change)
        return self.lib.submit(changes)

    def test_old_browse_immune_to_later_changes(self):
        # 80 条内容，优先级与批准时刻大量重叠
        changes = [
            make_change(i, priority=(i % 3) + 1,
                        first_approved_at=T0 + (i % 10))
            for i in range(1, 81)
        ]
        self.submit(changes)
        expected = self.model.catalog(T0 + 5_000)
        self.assertGreater(len(expected), 50)

        # 开始旧浏览，只读第一页
        old_page = self.lib.start_browse(T0 + 5_000)
        first_cursor = old_page.cursor
        self.assertEqual(len(old_page.items), 25)

        # 修改未读内容的优先级与正文、归档部分内容、新增内容
        mods = []
        for it in expected[25:50]:
            mods.append(ContentChange(item_id=it["item_id"],
                                      priority=5, body="rewritten"))
        for it in expected[50:60]:
            mods.append(ContentChange(item_id=it["item_id"],
                                      status="archived"))
        mods.append(make_change(999, priority=5))
        self.submit(mods)

        # 跨过到期边界：旧浏览的 T 不变，新浏览用窗口之后的时间
        old_items = list(old_page.items) + drain(
            self.lib, self.lib.continue_browse(first_cursor)
        )
        self.assertEqual(as_dicts(old_items), expected)

        # 新浏览反映最新提交与自己的 T
        fresh = drain(self.lib, self.lib.start_browse(T0 + 5_000))
        self.assertEqual(as_dicts(fresh), self.model.catalog(T0 + 5_000))
        expired = drain(self.lib, self.lib.start_browse(T0 + 20_000_000))
        self.assertEqual(expired, [])

    def test_interleaved_browses_and_cursor_reuse(self):
        self.submit([make_change(i, priority=(i % 2) + 1,
                                 first_approved_at=T0 + (i % 5))
                     for i in range(1, 121)])
        t = T0 + 100
        expected = self.model.catalog(t)

        page_a = self.lib.start_browse(t)
        page_b = self.lib.start_browse(t)
        collected_a = list(page_a.items)
        collected_b = list(page_b.items)

        # 交错翻页，并穿插内容变更
        cursor_a, cursor_b = page_a.cursor, page_b.cursor
        round_no = 0
        while cursor_a is not None or cursor_b is not None:
            if cursor_a is not None:
                page_a = self.lib.continue_browse(cursor_a)
                collected_a.extend(page_a.items)
                cursor_a = page_a.cursor
            self.submit([ContentChange(item_id=100 + round_no,
                                       body="changed-again")])
            if cursor_b is not None:
                page_b = self.lib.continue_browse(cursor_b)
                collected_b.extend(page_b.items)
                cursor_b = page_b.cursor
            round_no += 1

        self.assertEqual(as_dicts(collected_a), expected)
        self.assertEqual(as_dicts(collected_b), expected)

        # 同一游标重复使用返回相同一页，且不影响其他游标
        lib2_page = self.lib.start_browse(t)
        cursor = lib2_page.cursor
        page_once = self.lib.continue_browse(cursor)
        page_twice = self.lib.continue_browse(cursor)
        self.assertEqual(page_once, page_twice)
        expected = self.model.catalog(t)
        self.assertEqual(as_dicts(page_once.items),
                         expected[25:50])

    def test_window_boundaries(self):
        self.submit([
            make_change(1, window=(T0 + 100, T0 + 200)),
            make_change(2, window=(T0 + 99, T0 + 100)),
        ])
        # 起点包含：item 1 在 T0+100 可见
        page = self.lib.start_browse(T0 + 100)
        self.assertEqual([it.item_id for it in page.items], [1])
        # 终点排除：item 2 在 T0+100 已不可见；T0+99 时仅 item 2
        page = self.lib.start_browse(T0 + 99)
        self.assertEqual([it.item_id for it in page.items], [2])
        page = self.lib.start_browse(T0 + 200)
        self.assertEqual(list(page.items), [])

    def test_archived_and_unapproved_excluded(self):
        self.submit([
            make_change(1, status="approved"),
            make_change(2, status="archived"),
        ])
        page = self.lib.start_browse(T0 + 1)
        self.assertEqual([it.item_id for it in page.items], [1])

    def test_each_item_exactly_once(self):
        self.submit([make_change(i, priority=3, first_approved_at=T0)
                     for i in range(1, 101)])
        items = drain(self.lib, self.lib.start_browse(T0 + 1))
        ids = [it.item_id for it in items]
        self.assertEqual(len(ids), 100)
        self.assertEqual(sorted(ids), list(range(1, 101)))

    def test_unknown_cursor_rejected(self):
        with self.assertRaises(ValueError):
            self.lib.continue_browse("999:0:0:0")
        with self.assertRaises(ValueError):
            self.lib.continue_browse("not-a-cursor")


if __name__ == "__main__":
    unittest.main()
