---
id: RE-028
status: done
depends_on: [RE-003, RE-023, MD-003, MD-018]
adrs: [0078, 0079, 0080, 0081, 0082]
---

# 实现 Whole-Market Intraday Snapshot

## 结果

Market Research 能在实际执行边界一次性封存沪深 A 股全市场盘中观察，使用新浪批量主源和东方财富整包备源，并以明确覆盖率决定哪些 Market Insights 可发布。

## 范围

- 发布 `whole_market_intraday_snapshot@1` Data Product，包含预期 Market Securities、统一观察窗口、来源/抓取时间、价格与成交字段、缺失/停牌证据、整体及分组覆盖证明和不可变内容哈希。
- 建立 repository-owned 新浪 bulk 主适配器与东方财富 bulk 备适配器；两个适配器必须各自独立返回完整 observation，fallback 只替换整包，禁止逐股票或逐字段拼接。
- 使用批量分页/排行接口抓取全市场，不允许约 5000 次单股票 quote fan-out；所有页面必须属于同一有界 observation window，并记录首页/末页时间和页完整性。
- expected universe 只包含研究边界时活跃的沪深人民币普通股；已证明停牌或不在上市区间计为合法 absence，未知缺失计入 gap。
- readiness 要求未知缺失不超过 expected universe 的 1%，且在可用 Industry Sector Taxonomy 下每个一级行业有效覆盖至少 95%；始终输出实际覆盖率和 gap 数。
- 交易时段内必须取得接近执行边界的当前 snapshot；非交易时段使用最近已完成 session。缺失或陈旧数据不得伪装成当前情绪，相关 Insight 必须阻断。
- Manual Market Request 的 Boundary 与这次 one-shot Snapshot 开始/封存流程绑定；不建立连续 intraday collector 或历史分时回放服务。
- 本产品的新浪/东财 policy 与 Market Daily 的新浪唯一源完全隔离，不修改 MD-018 的日线运行路径。

## 不包含

- Industry membership 抓取、板块评分、Agent 方法、Market Information、连续行情服务或单股票 quote 替换。
- 在单元测试中访问真实新浪或东方财富网络。

## 验收条件

- [ ] 固定 fixtures 证明新浪完整时不调用备源，新浪整包失败时仅选用完整东财 observation。
- [ ] 任一页缺失、时间窗越界、字段冲突或覆盖不足时 fail closed，且绝不跨源补股票/字段。
- [ ] 99% 全市场与 95% 每行业阈值边界值、合法停牌、不在上市期和未知缺失均有确定测试。
- [ ] 交易中陈旧 snapshot 被拒绝；收盘后最近 completed session 可用，昨日收盘不能冒充盘中当前状态。
- [ ] Provider 请求数量随页数而非股票数增长，并受共享限流与有界重试控制。
- [ ] 选中 observation、attempts、时间、覆盖证明和 payload hash 可进入 sealed Research Data Snapshot。

## 可能触点

- `config/research/products/whole_market_intraday_snapshot.yaml`
- `advisor/research/providers/whole_market_intraday.py`
- `advisor/research/data_products/engine.py`
- `advisor/market_daily/universe.py`
- `tests/advisor/research/providers/test_whole_market_intraday.py`
- `tests/advisor/research/test_data_product_engine.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/providers/test_whole_market_intraday.py tests/advisor/research/test_data_product_engine.py'
```
