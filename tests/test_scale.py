"""规模用例：10 万内容、30 万变更、5000 个保留浏览、4000 页连续翻页。

可重复运行：pytest tests/test_scale.py -s  或  python3 tests/test_scale.py
报告：新增持久化字节数、峰值内存、完整翻页耗时，并校验最终集合与顺序。
"""

import os
import random
import resource
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sharelib import ContentLibrary, CreateContent, UpdateContent

N_CONTENTS = 100_000
N_UPDATES = 200_000          # 10 万新增 + 20 万修改 = 30 万次变更
N_BROWSES = 5_000
PAGE_SIZE = 25
EXPECTED_PAGES = N_CONTENTS // PAGE_SIZE  # 4000
MAX_PAGE_SECONDS = 15.0
MAX_MEMORY_BYTES = 128 * 1024 * 1024
MAX_BROWSE_BYTES = 5 * 1024 * 1024
BATCH = 1_000
BODY_LEN = 512

T_ALL_VISIBLE = 0            # 所有窗口都覆盖 T=0
WINDOW_END = 10**12


def make_body(seed_a: int, seed_b: int) -> str:
    head = f"{seed_a:06d}-{seed_b:06d}-"
    return (head + "abcdefgh" * BODY_LEN)[:BODY_LEN]


def dir_bytes(path: str) -> int:
    """目录下全部文件（含 SQLite 辅助文件）总字节数。"""
    total = 0
    for name in os.listdir(path):
        p = os.path.join(path, name)
        if os.path.isfile(p):
            total += os.path.getsize(p)
    return total


def peak_memory_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 返回字节，Linux 返回 KiB
    return rss if sys.platform == "darwin" else rss * 1024


def run_scale(workdir: str) -> dict:
    rng = random.Random(20261001)
    db_path = os.path.join(workdir, "scale.db")
    lib = ContentLibrary(db_path)
    report = {}

    # ---- 阶段 A：构造 10 万内容 + 30 万变更历史 ----
    t0 = time.perf_counter()
    priorities = [0] * (N_CONTENTS + 1)
    cid = 1
    while cid <= N_CONTENTS:
        batch = []
        for _ in range(BATCH):
            p = rng.randint(1, 5)
            priorities[cid] = p
            batch.append(CreateContent(
                title=f"content-{cid}", body=make_body(cid, 1), priority=p,
                first_approved_at=rng.randint(1, 10**6),
                status="approved",
                window_start=-cid,          # 每条起点互不相同 => 边界互不相同
                window_end=WINDOW_END,
                content_id=cid))
            cid += 1
        lib.commit(batch)
    for _ in range(N_UPDATES // BATCH):
        batch = []
        for _ in range(BATCH):
            target = rng.randint(1, N_CONTENTS)
            ver = rng.randint(2, 10**6)
            p = rng.randint(1, 5)
            priorities[target] = p
            batch.append(UpdateContent(target, body=make_body(target, ver),
                                       priority=p))
        lib.commit(batch)
    report["setup_seconds"] = time.perf_counter() - t0

    # ---- 阶段 B：10 万可见内容的浏览，连续读完 4000 页 ----
    expected = [r[0] for r in lib._conn.execute(
        "SELECT content_id FROM content_current"
        " WHERE status='approved' AND window_start <= ? AND window_end > ?"
        " ORDER BY priority DESC, first_approved_at DESC, content_id ASC",
        (T_ALL_VISIBLE, T_ALL_VISIBLE))]
    assert len(expected) == N_CONTENTS

    t0 = time.perf_counter()
    cursor = lib.start_browse(T_ALL_VISIBLE)
    seen_ids = []
    pages = 0
    while cursor is not None:
        page = lib.continue_browse(cursor)
        seen_ids.extend(it.content_id for it in page.items)
        cursor = page.next_cursor
        pages += 1
    page_seconds = time.perf_counter() - t0
    report["page_seconds"] = page_seconds
    report["pages"] = pages

    assert pages == EXPECTED_PAGES, pages
    assert len(seen_ids) == N_CONTENTS
    assert len(set(seen_ids)) == N_CONTENTS, "存在重复项"
    assert seen_ids == expected, "顺序与独立计算目录不一致"

    # ---- 阶段 C：5000 个不同窗口边界上开始浏览并保留 ----
    before_bytes = dir_bytes(workdir)
    kept = []
    # 边界取实际窗口起点：T = -(1 + k*18)，保证每处仍有 >= 10018 条可见
    for k in range(N_BROWSES):
        boundary = -(1 + k * 18)
        cur = lib.start_browse(boundary)
        first = lib.continue_browse(cur)
        assert len(first.items) == PAGE_SIZE  # 各边界处仍有足够内容继续翻页
        kept.append(first.next_cursor if first.next_cursor else cur)
    after_bytes = dir_bytes(workdir)
    browse_bytes = after_bytes - before_bytes
    report["browse_bytes"] = browse_bytes
    report["kept_browses"] = len(kept)

    # 保留的游标确实还能继续翻页（抽样 50 个）
    for cur in rng.sample(kept, 50):
        page = lib.continue_browse(cur)
        assert len(page.items) > 0

    peak = peak_memory_bytes()
    report["peak_memory_bytes"] = peak
    report["db_bytes_total"] = dir_bytes(workdir)

    lib.close()
    return report


def check_report(report: dict) -> None:
    assert report["page_seconds"] <= MAX_PAGE_SECONDS, report["page_seconds"]
    assert report["peak_memory_bytes"] <= MAX_MEMORY_BYTES, report["peak_memory_bytes"]
    assert report["browse_bytes"] <= MAX_BROWSE_BYTES, report["browse_bytes"]


def test_scale():
    workdir = tempfile.mkdtemp(prefix="sharelib-scale-")
    report = run_scale(workdir)
    print("\n==== 规模用例报告 ====")
    print(f"构造耗时(10万内容+30万变更): {report['setup_seconds']:.2f} s")
    print(f"翻页耗时(4000页/10万条):     {report['page_seconds']:.2f} s  (上限 {MAX_PAGE_SECONDS}s)")
    print(f"5000浏览新增持久化字节:      {report['browse_bytes']} B  (上限 {MAX_BROWSE_BYTES} B)")
    print(f"进程峰值内存:                {report['peak_memory_bytes']/1024/1024:.1f} MiB  (上限 128 MiB)")
    print(f"数据库总大小:                {report['db_bytes_total']/1024/1024:.1f} MiB")
    check_report(report)


if __name__ == "__main__":
    workdir = tempfile.mkdtemp(prefix="sharelib-scale-")
    rep = run_scale(workdir)
    for k, v in rep.items():
        print(f"{k}: {v}")
    check_report(rep)
    print("SCALE OK")
