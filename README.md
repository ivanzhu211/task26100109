# sharelib — 员工分享内容库（稳定分页）

纯 Python + SQLite 实现的本地库，由上层直接调用。内容及其全部修改历史存入
SQLite，浏览（翻页）对后续变更与时间流逝完全隔离。

## API

```python
from sharelib import ContentLibrary, CreateContent, UpdateContent

lib = ContentLibrary("contents.db")

# 1) 提交变更：一批变更原子生效，对新浏览整批可见
seq = lib.commit([
    CreateContent(title="海报A", body="...", priority=5,
                  first_approved_at=1_760_000_000_000, status="approved",
                  window_start=0, window_end=10**12, content_id=1),
    UpdateContent(content_id=2, body="新文案", priority=4),  # 部分字段修改
])

# 2) 开始浏览：以调用方给定的时刻 T（UTC 毫秒）固定目录
cursor = lib.start_browse(t_ms=1_760_000_100_000)

# 3) 继续浏览：每页 25 条；next_cursor 为 None 表示读完
while cursor is not None:
    page = lib.continue_browse(cursor)
    for item in page.items:      # ContentItem，含全部业务字段
        ...
    cursor = page.next_cursor
```

- 排序：优先级降序 → 首次批准时刻降序 → 编号升序；每页 25 条，末页可不足。
- 可见性：`status == "approved"` 且 `window_start <= T < window_end`
  （起点包含、终点排除）。
- `Cursor` 是不可变值对象：同一游标重复使用返回相同一页，互不影响推进。
- `first_approved_at` 不可修改（`UpdateContent` 不提供该字段）。
- 游标不要求跨进程重启有效。

## 设计

- `content_current`：每条内容的最新已提交版本（每内容一行）。
- `content_history`：全部历史版本（含最新版），`(content_id, version)` 主键。
- `commits`：每次提交一个单调递增 `commit_seq`，同批变更共享序号、整体原子生效。
- `browses`：每次浏览仅一行 `(browse_id, commit_seq, t_ms)`。不为浏览物化目录，
  因此 5000 个浏览的新增持久化数据只有几十 KB。
- 翻页为键集分页：排序键 `(priority 降, first_approved_at 降, content_id 升)`
  编码成单列字符串 `sort_key` 并建覆盖索引，每页是一次 `sort_key > 游标`
  的范围定位，只触碰约一页的索引行。
- 浏览开始后若发生新提交（慢路径）：结果 = 未变内容的当前版本（流 A，走索引）
  ∪ 变更内容截至浏览时的旧版本（流 B，从 `content_history` 取），
  两者按同一排序键在内存归并，保证旧浏览完全冻结。

## 测试

```bash
python3 -m pytest tests/ -q          # 全部（正确性 + 规模）
python3 -m pytest tests/test_correctness.py -q   # 仅正确性
python3 -m pytest tests/test_scale.py -q -s      # 仅规模用例（打印报告）
```

规模用例（10 万内容、30 万变更、5000 个保留浏览、4000 页连续翻页）
实测（本机）：翻页约 0.3s（上限 15s）、5000 浏览新增持久化约 68KB
（上限 5MiB）、峰值内存约 65MiB（上限 128MiB）。
