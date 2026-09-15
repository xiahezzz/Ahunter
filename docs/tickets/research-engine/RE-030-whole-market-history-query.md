---
id: RE-030
status: done
depends_on: [RE-008, RE-023, MD-013]
adrs: [0023, 0087, 0089]
---

# 扩展全市场历史查询能力

## 结果

Market Agent 能在同一只读、受预算和审计约束的 Research Query Interface 中，自主选择时间窗口并准确聚合研究边界前的全市场本地历史，而不把数百万行直接塞入模型提示词。

## 范围

- 发布 scope-compatible 的 `whole_market_daily_history@1` Data Product，暴露研究边界前本地已保留的完整 Canonical Daily Bars、停牌事实、上市区间、交易日和可推导研究价格视图。
- Snapshot 固定可查询历史的版本/集合证明；Query Interface 只能读取当前 Capsule 已声明产品，不能直接打开 live Advisor DB 或 unrestricted Artifact Store。
- 在共享 Query Interface 中增加有界的日期/代码过滤、group-by、count、sum/mean/median、breadth、rank/top/bottom 和窗口比较等确定性操作；Market 与 Security Agent 使用同一权限、预算、审计和错误契约。
- Agent 自己决定查询字段、窗口、分组、排序和分析方法；Engine 只准确执行请求，不提供统一市场/行业强度分或替 Agent 选择算法。
- 每次查询记录规范化参数、返回行数/字节数、结果哈希、耗时和累计预算；可引用结果形成 evidence，而不是依赖不可重放的模型算术。
- 查询始终执行 `as_of`、listing interval、absence 和 survivorship-safe 过滤；晚于边界的新摄取行不可见。
- 支持 Agent 在方法摘要中披露所用数据、时间窗口、条件、排名/分组依据和 evidence refs，不暴露 chain-of-thought。

## 不包含

- SQL shell、任意 Python、网络访问、写入能力、无限结果集、Engine-owned sector score 或另一个 Market-only Agent runtime。
- 扩大 Security Agent 已声明 Data Access，或让产品依赖自动变成可查询授权。

## 验收条件

- [ ] 规模 fixture 证明 Agent 可聚合全市场历史而 Capsule prompt 不包含全量原始行。
- [ ] 相同 pinned input 与 query 产生相同结果/哈希；越界时间、未授权产品和预算超限 fail closed。
- [ ] listing/delisting、停牌、新上市和边界后摄取数据不会造成 survivorship 或 future-evidence 泄漏。
- [ ] Market 与 Security Capsule 使用同一 Query 类、日志格式和预算规则，无 scope-specific 后门。
- [ ] group/rank/window 操作有数值精度、空集合、NaN/无限值、稳定排序和并列值测试。
- [ ] Query 结果可作为 evidence 被 Agent Finding 引用并由后续质量门验证。

## 可能触点

- `config/research/products/whole_market_daily_history.yaml`
- `advisor/research/providers/local.py`
- `advisor/research/query.py`
- `advisor/research/query_cli.py`
- `advisor/research/capsules.py`
- `tests/advisor/research/test_query_interface.py`
- `tests/advisor/research/providers/test_local_market.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/test_query_interface.py tests/advisor/research/providers/test_local_market.py tests/advisor/research/test_capsules.py'
```
