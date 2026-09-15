---
id: LE-003
status: in_progress
phase: phase_1
depends_on: [LE-001, LE-002]
---

# 历史数据包、规则费率包与覆盖预检

## 结果

取得可验证历史数据输入，并能明确回答原案例哪些证券、时段和执行场景能正式回放。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D03, D04, D05, D06）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-001](LE-001-specification-and-contracts.md), [LE-002](LE-002-records-artifacts-and-idempotency.md)

## 范围

- 实现 local_historical_bundle_v1 导入契约：来源/版本/哈希、事件/公开/采集时间、股票池、日历、原始日线/分钟、开收盘竞价、逐笔/队列、停复牌、企业行动。
- 独立实验导入不改变 Market Daily 新浪唯一实时源，不自动购买数据、连接备源或要求修改其密钥配置。
- 针对原例及预留验证/留出日期生成证券×交易日×证据能力覆盖报告；缺失、重复、矛盾时区、晚修订和复权污染逐项记录。
- 把当期沪深市场/板块/证券状态规则与法定税费固化为带生效区间和官方来源的版本包；核实交易资格、数量档位、涨跌幅、价格笼子、竞价/撤单时段和费用计法。
- 对现有 source_at=fetched_at 及 MX received_at 做能力清单；缺少历史版本证明不能倒填 available_at；不把今天证券池当历史池。
- 支持 fixture 验收和真实包验收两种明确标签；当前没有合格真实分钟/竞价包这一事实必须保留。

## 验收条件

- [ ] 日历解析真实得到请求天数；日期覆盖不足明确 blocked，不减少期间或股票池。
- [ ] 同一包重复导入幂等；哈希矛盾、缺规则或执行证据不够不能通过。
- [ ] 全市场必需覆盖报告与按场景证据报告均可导出；原例不能靠 daily bar 冒充 minute/queue ready。
- [ ] 真实来源包存在、时间语义可核对且覆盖通过，才可把本 ticket 标记真实数据验收完成；仅导入器与 fixture 完成要区分交付状态。

## 可能触点

advisor/research/experiments/data/（新增）；advisor/market_daily/contracts.py 的只读适配；reports/lagent-tests/ 数据预检产物

## 验证

离线固定包验证完整性/时间语义/历史规则；真实导入只读核对来源和覆盖，保留无收益 preflight 报告。

## CLI / skill 影响

新增导入/覆盖能力若通过 API 暴露，纳入 LE-012 catalog；不修改 Market Daily 来源语义。

## 2026-09-09 实现状态

内部导入契约、不可变来源封存、JSONL 索引、幂等与失败报告，以及证券×日期×场景
覆盖报告及其持久化导出已实现；26 项定向 fixture 测试通过。真实包、独立来源
审查和完整生效规则/法定费用包仍未验收，本 ticket 不标为 done。

源码时间语义与所检查目录的实际输入库存见
[数据能力报告](../../research/lagent-data-capability-status.md)。当前不放行正式运行；
其他 tickets 可继续用明确标记的 fixture 实现。临时停牌区间仍显式 unresolved，
完整逐笔与分钟/日线交叉核对、规则配置的完整官方语义及审查放行路径仍待补齐。
