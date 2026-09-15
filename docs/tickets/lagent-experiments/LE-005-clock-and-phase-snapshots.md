---
id: LE-005
status: in_progress
phase: phase_1
depends_on: [LE-001, LE-002, LE-003]
---

# 交易日历、阶段时钟与冻结快照

## 结果

按预定竞价前、竞价后、盘后阶段推进，任何现实延迟都不能让 agent 多看未来信息。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D03, D09）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-001](LE-001-specification-and-contracts.md), [LE-002](LE-002-records-artifacts-and-idempotency.md), [LE-003](LE-003-historical-data-and-rule-bundles.md)

## 范围

- 实现 env 的时钟状态与预定阶段列表；初始 23:05、交易日 09:24/09:25 事件界限及 23:05 复盘均由规格解析。
- 区分事件截止、快照可用、平台计划接收、交易所接受和回执时刻；竞价后快照默认 09:25:05，计划默认 09:29:00。
- 应用历史市场开闭/申报/撤单时段及 100ms 申报/回执延迟，不能把盘前接收等同交易所接受。
- 现实 call timeout/phase timeout 不推进模拟时钟；同阶段所有主/子任务固定同快照；非交易日不临时加阶段。
- 关闭阶段必须 fence 权限与迟到回调，09:30 至 23:00 不启动模型；末日盘后成本可计但收盘净值不动。

## 验收条件

- [ ] 竞价后计划不能参与已结束开盘竞价；09:25 之后新新闻不因 09:25:05 快照而可见。
- [ ] 竞价结果或必要回执晚于固定快照时明确质量受阻，不动态推迟窗口。
- [ ] 模型现实运行跨午夜、重试或子任务晚回均不扩展观察界限。
- [ ] 任意合法持续天数和休市日可解析；窗口冲突或市场不接受的申报时序被识别。

## 可能触点

advisor/research/experiments/clock.py（新增）；advisor/research/experiments/env.py（新增）

## 验证

通过 create_episode/observe/advance 检查时间边界、休市、延迟和 late result；使用可控时钟。

## CLI / skill 影响

CLI/trace 输出需同时显示模拟阶段/截点与现实耗时；不得显示为真实交易执行时间承诺。

## 2026-09-09 内部阶段时钟实现

已新增 `clock.py`、`timing.py`，复用共享事件、投影和 worker lease；PhaseSession 已
绑定真实持久阶段权限，替代仅 fixture 回调的接入方式。24 项新增时钟/快照/接收时序
测试通过，具体边界见[阶段时钟契约](../../research/lagent-phase-clock-contracts.md)。

本 ticket 保持 in_progress：完整 create_episode/observe/advance 运行入口和子进程关闭
确认尚待 LE-010/008；实际账户收盘估值不受盘后研究影响须随 LE-006 集成验收。
内部时钟、冻结与权限测试不替代真实市场规则或可运行 Episode 验收。
