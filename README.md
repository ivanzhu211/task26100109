# 员工分享内容库（稳定分页）

Python + SQLite 实现的内容库，由上层直接实例化 `ContentLibrary` 调用。
内容与全部修改历史持久化在 SQLite，浏览（browse）状态仅保存在进程内存中。

## API

```python
from content_library import ContentLibrary, ContentChange

lib = ContentLibrary("content.db")

# 1. 提交变更：一批变更原子生效，返回新版本号
lib.submit([
    ContentChange(item_id=1, title="t", body="b", priority=3,
                  status="approved", window_start=0, window_end=10**12,
                  first_approved_at=1_700_000_000_000),   # 新建：全部字段必填
    ContentChange(item_id=2, priority=5),                  # 修改：只给变更字段
])

# 2. 开始浏览：以时刻 T（UTC 毫秒）固定目录快照，返回第一页
page = lib.start_browse(t_ms)

# 3. 继续浏览：用上一页的游标取下一页；cursor 为 None 表示结束
while page.cursor is not None:
    page = lib.continue_browse(page.cursor)
```

- 每页 25 条，末页可不足；排序：优先级降序、首次批准时刻降序、编号升序。
- 仅纳入已批准且 `window_start <= T < window_end` 的内容。
- 浏览开始即固定内容集合、顺序与全部返回字段；之后的修改、归档、
  新增与时间流逝都不影响旧浏览，新浏览使用最新提交与自己的 T。
- 同一游标可重复使用，返回相同一页，且不影响其他游标。
- `first_approved_at` 不可修改，其余业务字段可改；所有时刻为 UTC 毫秒。

## 设计要点

- `revisions(item_id, version, ...)` 保存每次提交的完整状态，
  `items` 保存不可变的首次批准时刻，`meta` 保存版本计数；
  一批变更共用一个新版本号，单事务提交，保证整批可见。
- `current` 表保存最新状态，并带有 `(sort_pri, sort_fa, item_id)`
  索引（`sort_pri=-priority`、`sort_fa=-first_approved_at`，
  把混合升降序统一为升序）。浏览版本等于当前版本时，
  用行值键集分页 `(sort_pri, sort_fa, item_id) > (?,?,?)`
  走索引定位，每页一次 `LIMIT 25` 查询。
- 版本落后于当前版本的旧浏览改走 `revisions` 历史查询
  （每条目取 `version <= V` 的最大版本），保证快照语义。
- 浏览不在 SQLite 写入任何数据：游标只编码
  `浏览编号:sort_pri:sort_fa:item_id`，浏览元数据
  （版本、时刻 T）在内存中，因此 5000 个保留浏览的持久化增量为 0。

## 测试

```bash
python3 -m unittest discover -s tests   # 全部测试
python3 tests/test_scale.py             # 仅规模用例（可重复运行）
```

- `tests/test_correctness.py`：快照稳定性（读第一页后修改优先级/正文、
  归档、跨过到期边界，旧浏览须与开始时独立计算的目录逐项一致）、
  优先级与批准时刻大面积相同、多浏览交错翻页、游标重复使用、
  窗口起点包含/终点排除、每条内容恰好出现一次。
- `tests/test_scale.py`：10 万条内容、30 万次修改、5000 个保留浏览，
  校验最终集合与顺序，并断言持久化增量 ≤ 5 MiB、
  峰值内存 ≤ 128 MiB、4000 页连读 ≤ 15s。

本机实测：构建 8.0s；5000 个浏览新增持久化 0 字节；
峰值内存 47.7 MiB；4000 页连读 0.47s。
