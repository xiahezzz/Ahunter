---
id: LE-004
status: in_progress
phase: phase_1
depends_on: [LE-001, LE-002, LE-003]
---

# 历史数据访问与日期受控搜索

## 结果

主/子 agent 的所有数据查询受同一历史界限约束，搜索遵守已接受的服务日期过滤信任政策。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D04, D07）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-001](LE-001-specification-and-contracts.md), [LE-002](LE-002-records-artifacts-and-idempotency.md), [LE-003](LE-003-historical-data-and-rule-bundles.md)

## 范围

- 实现 env.query 与产品 available_at 映射，区分事件截止、发布时间、采集时间和版本可见性；衍生产品取全部依赖可用时间上界。
- 实现 Tavily 日期过滤适配器；平台暴露 last_included_date 并注入截止，fixture 固定 end_date 包含性/时区映射；不能可靠映射时收紧并留痕。
- 仅通过受控连接引用使用服务，不持久化密钥、不自动购买；没有连接返回 search_provider_unavailable，现有业务 LAgent 内置搜索保持原策略。
- 记录查询参数、截点、结果、provider 版本、缓存世代和 trusted/verified 时间状态；禁用自动参数和生成式答案。
- 封堵直接 URL/重定向/模型内置搜索/shell 网络绕过；结果是证据而非系统指令；可选产品失败与必需证据失败分开。
- 实现同世代查询响应缓存、截止隔离及外部原文过期标记；不同候选不可读取对方查询清单。

## 验收条件

- [ ] 08-02 盘后日期搜索不含 08-02；当日精确时间信息仅从合格产品进入。
- [ ] 显式未来结果拒绝；无日期结果按服务信任政策标注，不强制逐页存档且不误标已验证时间。
- [ ] 相同截止缓存可回放；新世代、政策/日期变化不能错误复用。
- [ ] 子 agent、错误消息、目录列表和 trace 均不能泄露未来行情或隐藏任务数据。

## 可能触点

advisor/research/experiments/data/；advisor/research/experiments/search.py（新增）；advisor/research/codex/policy.py 的实验策略适配

## 验证

env.query 的可见性与逃逸测试使用固定响应；真实连接能力检查与真实日期参数验收在 LE-015 留痕。

## CLI / skill 影响

新增搜索配置和能力错误必须经 CLI 显示；skill 描述日期精度和连接缺失，不宣称逐页历史验证。

## 2026-09-09 内部实现进度

`data/temporal.py`、`data/access.py` 与 `search.py` 已实现时间映射、固定界限查询、
主子会话继承、通用错误、搜索日期映射、受控 HTTP 连接、不可变世代缓存和正文过期重放。
26 项新增测试与 LE-003 组合共 52 项通过；详见
[接口及验收边界](../../research/lagent-time-access-contracts.md)。

本 ticket 保持 in_progress：PhaseSession 目前由宿主注入权限校验回调，LE-005 的持久阶段
生命周期尚未连接；完整 OS/shell/网络隔离须在 LE-008 验收。真实 Tavily 连接、日期语义与
费用门槛仍未验收，测试只用明确 fixture，不调用真实服务。
