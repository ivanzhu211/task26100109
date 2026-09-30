"""正确性测试：浏览快照隔离、并列排序、交错翻页、游标复用、窗口边界。"""

import random

import pytest

from sharelib import (
    ContentLibrary,
    CreateContent,
    Cursor,
    Page,
    UpdateContent,
)


class Oracle:
    """独立预言机：在 Python 内存中重放全部提交，计算任一 (seq, T) 的目录。"""

    def __init__(self):
        self.versions = {}  # content_id -> list[(seq, fields)]

    def commit(self, seq, changes):
        for ch in changes:
            if isinstance(ch, CreateContent):
                self.versions.setdefault(ch.content_id, []).append(
                    (seq, dict(title=ch.title, body=ch.body, priority=ch.priority,
                               first_approved_at=ch.first_approved_at,
                               status=ch.status, window_start=ch.window_start,
                               window_end=ch.window_end)))
            else:
                hist = self.versions[ch.content_id]
                cur = dict(hist[-1][1])
                for f in ("title", "body", "priority", "status",
                          "window_start", "window_end"):
                    v = getattr(ch, f)
                    if v is not None:
                        cur[f] = v
                hist.append((seq, cur))

    def directory(self, seq, t_ms):
        """截至 seq、时刻 t_ms 的可见目录（完整字段），按浏览排序。"""
        out = []
        for cid, hist in self.versions.items():
            vis = None
            for s, fields in hist:
                if s <= seq:
                    vis = fields
                else:
                    break
            if vis is None:
                continue
            if vis["status"] == "approved" and vis["window_start"] <= t_ms < vis["window_end"]:
                out.append((cid, vis))
        out.sort(key=lambda kv: (-kv[1]["priority"], -kv[1]["first_approved_at"], kv[0]))
        return out


def read_all(lib, cursor):
    items = []
    while cursor is not None:
        page = lib.continue_browse(cursor)
        items.extend(page.items)
        cursor = page.next_cursor
    return items


def make_create(i, rng, window=(0, 10_000)):
    return CreateContent(
        title=f"title-{i}", body=f"body-{i}-v1",
        priority=rng.randint(1, 5),
        first_approved_at=rng.choice([1000, 2000, 3000]),
        status="approved",
        window_start=window[0], window_end=window[1],
        content_id=i,
    )


@pytest.fixture()
def lib(tmp_path):
    lib = ContentLibrary(str(tmp_path / "test.db"))
    yield lib
    lib.close()


def test_old_browse_isolated_from_later_changes(lib):
    """验收场景：读一页后修改未读内容、归档部分、跨过到期边界。"""
    rng = random.Random(42)
    oracle = Oracle()
    n = 120
    creates = [make_create(i, rng, window=(0, 5000)) for i in range(1, n + 1)]
    seq1 = lib.commit(creates)
    oracle.commit(seq1, creates)

    t0 = 1000
    cur = lib.start_browse(t0)
    first_page = lib.continue_browse(cur)
    assert len(first_page.items) == 25

    # 营销团队修改：未读内容改优先级与正文、归档一部分、窗口挪走一部分
    changes = []
    for i in range(30, 60):
        changes.append(UpdateContent(i, body=f"body-{i}-v2",
                                     priority=rng.randint(1, 5)))
    for i in range(60, 80):
        changes.append(UpdateContent(i, status="archived"))
    for i in range(80, 90):
        changes.append(UpdateContent(i, window_start=99999, window_end=100000))
    # 新增内容
    changes.append(CreateContent("new", "nb", 5, 500, "approved", 0, 5000,
                                 content_id=n + 1))
    seq2 = lib.commit(changes)
    oracle.commit(seq2, changes)

    # 旧浏览剩余页
    rest = read_all(lib, first_page.next_cursor)
    old_all = list(first_page.items) + rest

    expected = oracle.directory(seq1, t0)
    assert len(old_all) == len(expected)
    for item, (cid, fields) in zip(old_all, expected):
        assert item.content_id == cid
        assert item.body == fields["body"]
        assert item.priority == fields["priority"]
        assert item.title == fields["title"]
        assert item.first_approved_at == fields["first_approved_at"]
        assert item.status == fields["status"]
        assert (item.window_start, item.window_end) == (
            fields["window_start"], fields["window_end"])

    # 每条恰好出现一次
    ids = [it.content_id for it in old_all]
    assert len(ids) == len(set(ids))

    # 新浏览（跨过到期边界：T 超过原窗口终点）反映最新提交
    t1 = 6000
    new_items = read_all(lib, lib.start_browse(t1))
    expected_new = oracle.directory(seq2, t1)
    assert [it.content_id for it in new_items] == [cid for cid, _ in expected_new]
    # 新浏览里看不到已归档与窗口未覆盖的内容
    assert not any(60 <= it.content_id < 80 for it in new_items)
    assert any(it.content_id == n + 1 for it in new_items) is False  # 新内容窗口 0..5000，t1=6000 已过期
    # 在窗口内的新浏览可见修改后的正文
    new_items2 = read_all(lib, lib.start_browse(1000))
    by_id = {it.content_id: it for it in new_items2}
    assert by_id[30].body == "body-30-v2"
    assert n + 1 in by_id


def test_ties_and_interleaved_browses_and_cursor_reuse(lib):
    """大量并列优先级与批准时刻、多浏览交错翻页、旧游标复用。"""
    rng = random.Random(7)
    n = 200
    creates = [
        CreateContent(f"t{i}", f"b{i}", priority=rng.choice([1, 2]),
                      first_approved_at=rng.choice([100, 100, 200]),
                      status="approved", window_start=0, window_end=10**12,
                      content_id=i)
        for i in range(1, n + 1)
    ]
    lib.commit(creates)

    cursors = [lib.start_browse(500) for _ in range(4)]
    pages = [[] for _ in cursors]
    active = list(range(len(cursors)))
    # 交错翻页
    while active:
        for idx in list(active):
            page = lib.continue_browse(cursors[idx])
            pages[idx].append(page)
            if page.next_cursor is None:
                active.remove(idx)
            else:
                cursors[idx] = page.next_cursor

    # 每个浏览都完整、一致、无重复
    expected_ids = None
    for pgs in pages:
        items = [it for p in pgs for it in p.items]
        ids = [it.content_id for it in items]
        assert len(ids) == n
        assert len(set(ids)) == n
        keys = [(-it.priority, -it.first_approved_at, it.content_id) for it in items]
        assert keys == sorted(keys)
        if expected_ids is None:
            expected_ids = ids
        else:
            assert ids == expected_ids

    # 旧游标复用：返回相同一页，且不影响其他游标
    cur_a = lib.start_browse(500)
    p1 = lib.continue_browse(cur_a)
    p2 = lib.continue_browse(p1.next_cursor)
    # 复用第一页的游标
    p1_again = lib.continue_browse(cur_a)
    assert p1_again.items == p1.items
    assert p1_again.next_cursor == p1.next_cursor
    # 复用第二页游标
    p2_again = lib.continue_browse(p1.next_cursor)
    assert p2_again.items == p2.items
    # 从第二页继续走到结束，与一次性读完的结果一致
    rest = read_all(lib, p2.next_cursor)
    total = list(p1.items) + list(p2.items) + rest
    assert [it.content_id for it in total] == expected_ids


def test_window_boundaries(lib):
    """窗口起点包含、终点排除。"""
    lib.commit([
        CreateContent("a", "x", 3, 100, "approved", 1000, 2000, content_id=1),
        CreateContent("b", "x", 3, 100, "approved", 2000, 3000, content_id=2),
    ])
    # 起点包含
    assert [i.content_id for i in read_all(lib, lib.start_browse(1000))] == [1]
    assert [i.content_id for i in read_all(lib, lib.start_browse(2000))] == [2]
    # 终点排除
    assert read_all(lib, lib.start_browse(3000)) == []
    assert read_all(lib, lib.start_browse(999)) == []
    # 中间
    assert [i.content_id for i in read_all(lib, lib.start_browse(1500))] == [1]


def test_batch_atomicity_and_visibility(lib):
    """一次提交的多条变更对新浏览整批可见；失败批次整体回滚。"""
    lib.commit([CreateContent("a", "x", 3, 100, "approved", 0, 100, content_id=1)])
    seq = lib.commit([
        UpdateContent(1, body="v2"),
        CreateContent("b", "y", 4, 200, "approved", 0, 100, content_id=2),
        CreateContent("c", "z", 5, 300, "approved", 0, 100, content_id=3),
    ])
    items = read_all(lib, lib.start_browse(50))
    assert [i.content_id for i in items] == [3, 2, 1]
    assert items[-1].body == "v2"

    # 批次中一条失败 => 整批不可见
    with pytest.raises(ValueError):
        lib.commit([
            UpdateContent(1, body="v3"),
            UpdateContent(999, body="nope"),
        ])
    items = read_all(lib, lib.start_browse(50))
    assert items[-1].body == "v2"


def test_first_approved_at_immutable(lib):
    """UpdateContent 不提供 first_approved_at 字段，且历史保留原始值。"""
    lib.commit([CreateContent("a", "x", 3, 12345, "approved", 0, 100, content_id=1)])
    lib.commit([UpdateContent(1, body="v2", priority=5)])
    items = read_all(lib, lib.start_browse(50))
    assert items[0].first_approved_at == 12345
    rows = lib._conn.execute(
        "SELECT DISTINCT first_approved_at FROM content_history WHERE content_id=1"
    ).fetchall()
    assert rows == [(12345,)]


def test_empty_browse_and_empty_page(lib):
    lib.commit([CreateContent("a", "x", 3, 100, "archived", 0, 100, content_id=1)])
    cur = lib.start_browse(50)
    page = lib.continue_browse(cur)
    assert page.items == ()
    assert page.next_cursor is None


def test_exact_page_boundary(lib):
    """总数恰为页大小整数倍时，末页之后正确结束。"""
    creates = [CreateContent(f"t{i}", "b", 3, 100, "approved", 0, 100, content_id=i)
               for i in range(1, 51)]
    lib.commit(creates)
    cur = lib.start_browse(50)
    p1 = lib.continue_browse(cur)
    assert len(p1.items) == 25 and p1.next_cursor is not None
    p2 = lib.continue_browse(p1.next_cursor)
    assert len(p2.items) == 25 and p2.next_cursor is None


def test_randomized_interleaving_with_oracle(lib):
    """随机交错：多浏览在不同提交点开启，变更与翻页交错，全程对照预言机。"""
    rng = random.Random(2026)
    oracle = Oracle()
    n = 300
    creates = [
        CreateContent(f"t{i}", f"b{i}-v1", priority=rng.randint(1, 5),
                      first_approved_at=rng.choice([100, 200, 200, 300]),
                      status="approved",
                      window_start=rng.choice([0, 500]),
                      window_end=rng.choice([1500, 2500, 10**12]),
                      content_id=i)
        for i in range(1, n + 1)
    ]
    seq = lib.commit(creates)
    oracle.commit(seq, creates)

    # 三个浏览在不同提交点、不同 T 开启
    browses = {}  # name -> (seq_at_start, t_ms, cursor)
    for name, t_ms in (("a", 100), ("b", 1000)):
        cur = lib.start_browse(t_ms)
        browses[name] = (lib._latest_seq, t_ms, cur)

    # 交错：两轮修改 + 两次开新浏览
    for round_no in range(2):
        changes = []
        for _ in range(40):
            target = rng.randint(1, n)
            field = rng.choice(["body", "priority", "status", "window"])
            if field == "body":
                changes.append(UpdateContent(target, body=f"b{target}-r{round_no}"))
            elif field == "priority":
                changes.append(UpdateContent(target, priority=rng.randint(1, 5)))
            elif field == "status":
                changes.append(UpdateContent(
                    target, status=rng.choice(["approved", "archived"])))
            else:
                ws, we = rng.choice([(0, 1500), (500, 2500), (5000, 10**12)])
                changes.append(UpdateContent(target, window_start=ws,
                                             window_end=we))
        seq = lib.commit(changes)
        oracle.commit(seq, changes)
        cur = lib.start_browse(1000)
        browses[f"new{round_no}"] = (lib._latest_seq, 1000, cur)

    # 每个浏览完整读完，与预言机逐项对照
    for name, (seq_at, t_ms, cur) in browses.items():
        items = read_all(lib, cur)
        expected = oracle.directory(seq_at, t_ms)
        got_ids = [it.content_id for it in items]
        assert got_ids == [cid for cid, _ in expected], name
        assert len(got_ids) == len(set(got_ids)), name
        for item, (cid, fields) in zip(items, expected):
            assert item.body == fields["body"], (name, cid)
            assert item.priority == fields["priority"], (name, cid)
            assert item.status == fields["status"], (name, cid)
