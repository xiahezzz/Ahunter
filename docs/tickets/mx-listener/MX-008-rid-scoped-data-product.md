---
id: MX-008
status: complete
depends_on: [MX-004, MX-005]
adrs: [0005, 0012, 0018, 0023, 0063, 0064, 0067]
---

# 建立 RID-scoped `mx_events@2` 与 Snapshot 隔离

## 结果

Research Engine 把每个明确授权给 Agent 的 RID 物化为独立、可哈希、可追溯的 MX RID Feed，在共享 Snapshot 中只构建一次，并向每个 Agent 暴露其精确子集。

## 范围

- 保留 `mx_events@1` 与现有 Agent Manifest 不变；新增不可变 `mx_events@2` Manifest 和规范化 result schema，不覆盖已使用定义。
- 新建 `LocalMxProvider` Adapter，通过 MX-005 的 Accepted Event 读取接口获取本地事实；`public-a-share` 不再承担 v2 本地 MX 读取逻辑。
- `mx_events@2` 使用 Manifest 固定的 30 日半开时间窗 `(as_of - 30 days, as_of]`，每个 RID 最多 5,000 条；超过上限明确阻断该 Feed，不静默截断。
- 每个 Feed 单独包含 RID、时间窗、事件数、质量、内容哈希和安全 normalized items；事件 item 不含 raw payload、source URL、本地路径或浏览器标识。
- 空 Feed 是合法、质量通过的观察结果；消息静默不等于缺失或授权失败。
- Snapshot Planner 计算所有选定 Agent Data Access 的精确 RID 并集；每个 RID Feed 只读取、规范化和持久化一次，再由产品 index 引用独立 artifact hash。
- 请求的 RID 必须在当前 RID Authorization Set；已撤销、配置无效或未经授权的 Feed 标记 unavailable，不回退到 retained history。
- 媒体 pending/failed 按 RID 影响对应 Feed；无法归属 RID 的 decode/integrity failure 在同一时间窗内阻断整个 MX 产品，并记录有限质量原因。
- Query Interface 只能枚举和检索当前 Invocation 声明的 Feed 子集；联合 Snapshot、其他 Agent Feed 和事件库路径都不可见。
- Invocation Key 包含实际可见 Feed artifact hashes；相同 Agent/Subject/as_of/Feed 集合可复用，不同 Feed 权限不能误命中同一 Invocation。

## 不包含

- per-RID Product Manifest、per-RID Provider、动态“全部未来 RID”、模糊股票相关性判断或模型选择来源。
- 修改现有 Social Agent 或 Team；通过 WebUI 发布新 Agent/Team 版本属于 MX-009～MX-011。

## 验收条件

- [x] 两个 RID 生成两个独立 artifact hash 和一个产品 index；任一内容变化不改写另一个 Feed artifact。
- [x] 两个 Agent 使用重叠 Feed 时 union 只物化一次，但各自 Capsule 只能查询声明子集。
- [x] 新增授权 RID 不进入旧 Agent 的 Snapshot；已撤销 RID 不使用 retained history并只阻断依赖方。
- [x] 空 Feed 通过质量检查；超上限、未来事件、无授权、媒体失败和全局 integrity failure 有稳定 fail-closed 测试。
- [x] `mx_events@1`、现有 Social Agent、旧 Snapshot 和旧报告仍可读取且字节未被覆盖。
- [x] Feed/产品/Capsule artifact 均不含 raw payload、原始媒体 URL、本地路径或当前完整 Authorization Set。
- [x] 相同输入产生相同 feed/product/snapshot hash 和 Invocation Key。

## 可能触点

- `config/research/products/mx_events_v2.yaml`
- `advisor/research/providers/local_mx.py`
- `advisor/research/data_products/engine.py`
- `advisor/research/agents/runner.py`
- `advisor/research/capsules.py`
- `advisor/research/query.py`
- `tests/advisor/research/test_mx_rid_feeds.py`
- `tests/advisor/research/test_data_product_engine.py`
- `tests/advisor/research/test_query_interface.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/test_mx_rid_feeds.py tests/advisor/research/test_data_product_engine.py tests/advisor/research/test_query_interface.py'
```
