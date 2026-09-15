---
id: MD-006
status: complete
depends_on: [MD-004, MD-005]
adrs: [0032, 0044]
---

# 实现 Observed Trading Sessions

## 结果

系统以两套独立基准指数日线的共同观察维护本地交易日事实，21:00 运行不再依赖只能覆盖 2025–2026 年的硬编码节假日表。

## 范围

- 为沪深基准指数定义窄的 Session Observation 契约，复用 Eastmoney 与 TDX 连接边界。
- 主源与备源成功观察到同一最新完成日期后，才写入 `trading_sessions`。
- 冷启动按五年窗口物化全部观察到的交易日，日期严格升序唯一。
- 最新观察日期未变化是成功 no-op；任一必需来源不可用或两者日期冲突是失败，不得猜测休市。
- 21:00 前后的 `Asia/Shanghai` 边界有确定性时钟测试，拒绝未来交易日。
- 用本地 session repository 替换 `advisor/calendar.py` 的有限年份判断；保留窄兼容调用面，调用者不读取常量假日集合。
- 为质量门提供“最新已证明交易日”和窗口 session 列表查询。

## 不包含

- 个股停牌判断、K 线冷启动、Service 定时器或研究指标。

## 验收条件

- [x] 两源一致、日期未变化、主源失败、备源失败和日期冲突均有测试。
- [x] 周末与休市日通过“最新 session 未变化”成为 no-op，而非硬编码日期。
- [x] 本地交易日表可完整表达五年窗口并生成稳定哈希。
- [x] 项目不再因日期晚于 2026-12-31 抛出有限日历错误。
- [x] 质量门无法把来源故障误判为休市。
- [x] 自动化测试不访问外部网络。

## 可能触点

- `advisor/market_daily/sessions.py`
- `advisor/calendar.py`
- `advisor/quality.py`
- `tests/advisor/market_daily/test_sessions.py`
- `tests/advisor/test_quality_gate.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/market_daily/test_sessions.py \
  tests/advisor/test_quality_gate.py
```
