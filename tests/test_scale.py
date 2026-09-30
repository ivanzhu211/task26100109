"""规模用例：10 万内容、30 万修改、5000 个保留浏览、4000 页连读。

可重复运行：python3 tests/test_scale.py
报告新增持久化字节数、峰值内存与完整翻页耗时，并校验最终集合与顺序。
"""
import os
import resource
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from content_library import ContentChange, ContentLibrary

N_ITEMS = 100_000
N_MODS = 300_000
N_BROWSES = 5_000
PAGE_SIZE = 25
N_PAGES = N_ITEMS // PAGE_SIZE  # 4000
BODY_LEN = 512
BASE = 10**12
WINDOW_SPAN = 10_000  # 窗口终点取值个数，浏览用前 5000 个边界
MAX_PERSISTENT_GROWTH = 5 * 1024 * 1024
MAX_MEMORY = 128 * 1024 * 1024
MAX_READ_SECONDS = 15.0


def body_of(item_id, seq):
    return f"{item_id}:{seq}".encode().ljust(BODY_LEN, b"x").decode()


def first_approved_of(item_id):
    return 1_000_000 + (item_id % 50_000)


def window_of(item_id):
    return 0, BASE + (item_id % WINDOW_SPAN) * 1000


def dir_bytes(path):
    total = 0
    for name in os.listdir(path):
        total += os.path.getsize(os.path.join(path, name))
    return total


def peak_rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


class ScaleTest(unittest.TestCase):
    def test_scale(self):
        tmp = tempfile.mkdtemp(prefix="contentlib-scale-")
        try:
            self._run(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _run(self, tmp):
        db_path = os.path.join(tmp, "content.db")
        lib = ContentLibrary(db_path)

        # 阶段 1：创建 10 万条内容
        t0 = time.monotonic()
        BATCH = 1000
        for start in range(0, N_ITEMS, BATCH):
            lib.submit(
                ContentChange(
                    item_id=i,
                    title=f"title-{i}",
                    body=body_of(i, 0),
                    priority=(i % 5) + 1,
                    status="approved",
                    window_start=window_of(i)[0],
                    window_end=window_of(i)[1],
                    first_approved_at=first_approved_of(i),
                )
                for i in range(start, start + BATCH)
            )
        # 阶段 2：累计 30 万次修改（确定性：改优先级，部分改正文）
        priority = bytearray((i % 5) + 1 for i in range(N_ITEMS))
        seq = [0] * N_ITEMS
        body_seq = [0] * N_ITEMS
        mod_no = 0
        while mod_no < N_MODS:
            batch = []
            for _ in range(min(BATCH, N_MODS - mod_no)):
                item_id = (mod_no * 7) % N_ITEMS
                seq[item_id] += 1
                priority[item_id] = (mod_no % 5) + 1
                change = ContentChange(
                    item_id=item_id, priority=priority[item_id]
                )
                if mod_no % 10 == 0:
                    body_seq[item_id] = seq[item_id]
                    change = ContentChange(
                        item_id=item_id,
                        priority=priority[item_id],
                        body=body_of(item_id, seq[item_id]),
                    )
                batch.append(change)
                mod_no += 1
            lib.submit(batch)
        build_seconds = time.monotonic() - t0

        # 阶段 3：5000 个窗口边界上开始浏览并保留游标
        before_bytes = dir_bytes(tmp)
        kept = []
        for k in range(N_BROWSES):
            t_k = BASE + k * 1000  # 恰好是若干内容的窗口终点边界
            page = lib.start_browse(t_k)
            self.assertEqual(len(page.items), PAGE_SIZE)
            self.assertIsNotNone(page.cursor)
            kept.append(page.cursor)
        after_bytes = dir_bytes(tmp)
        growth = after_bytes - before_bytes

        # 保留的游标仍可继续翻页（抽查）
        for cursor in kept[::500]:
            page = lib.continue_browse(cursor)
            self.assertEqual(len(page.items), PAGE_SIZE)

        # 阶段 4：10 万条可见内容的浏览连读 4000 页并校验
        expected_ids = sorted(
            range(N_ITEMS),
            key=lambda i: (-priority[i], -first_approved_of(i), i),
        )
        t0 = time.monotonic()
        page = lib.start_browse(BASE - 1)
        pos = 0
        pages = 0
        while page.cursor is not None or page.items:
            for item in page.items:
                i = expected_ids[pos]
                self.assertEqual(item.item_id, i)
                self.assertEqual(item.priority, priority[i])
                self.assertEqual(item.body, body_of(i, body_seq[i]))
                self.assertEqual(
                    item.first_approved_at, first_approved_of(i)
                )
                pos += 1
            pages += 1
            if page.cursor is None:
                break
            page = lib.continue_browse(page.cursor)
        read_seconds = time.monotonic() - t0
        self.assertEqual(pos, N_ITEMS)
        self.assertEqual(pages, N_PAGES)

        rss = peak_rss()
        print(
            f"\n[scale] 构建 {N_ITEMS} 条内容 + {N_MODS} 次修改: "
            f"{build_seconds:.1f}s"
        )
        print(f"[scale] 5000 个浏览新增持久化字节: {growth}")
        print(f"[scale] 进程峰值内存: {rss / 1024 / 1024:.1f} MiB")
        print(f"[scale] 连读 {N_PAGES} 页耗时: {read_seconds:.2f}s")
        self.assertLessEqual(growth, MAX_PERSISTENT_GROWTH)
        self.assertLessEqual(rss, MAX_MEMORY)
        self.assertLessEqual(read_seconds, MAX_READ_SECONDS)
        lib.close()


if __name__ == "__main__":
    unittest.main()
