---
id: RE-011
status: done
depends_on: [RE-008, RE-009]
adrs: [0002, 0007, 0014, 0020, 0022]
---

# 实现公共 Decision Pipeline

## 结果

每个完整 Team 使用完全相同的固定 Pipeline，将 Findings 转换为独立 Team Conclusion；不存在循环对话、Team 定制阶段或跨 Team 输入。

## 固定拓扑

```text
Finding quality gate
        ↓
Bull Review ── Bear Review
        ↓
Research Manager
        ↓
Trader
        ↓
Aggressive ── Neutral ── Conservative Risk Reviews
        ↓
Portfolio Manager
        ↓
Publication quality gate
```

Bull/Bear 读取相同 Findings 并行执行；三个风险 Review 读取相同 Trader Proposal 并行执行。Peer stages 互不读取，只有下游 Manager 同时接收它们。

## 范围

- 为 Bull Review、Bear Review、Research Plan、Trader Proposal、三类 Risk Review 和 Team Conclusion 定义版本化 Schema。
- 实现固定 Stage Runner 与上下文白名单，每个生成式 Stage 使用独立 Run Capsule 和 Codex 任务。
- 实现 Finding 输入质量门和 Team Conclusion 发布质量门。
- Team Conclusion 输出五档 Decision Stance、定性 Decision Conviction、确定性 evidence quality、thesis、证据引用、主要风险、失效条件、时间范围及可选价格/仓位边界。
- Pipeline 版本进入运行指纹，所有 Teams 在同一 Cycle 使用同一版本。

## 不包含

- 数值 confidence、订单对象、账本写入或券商调用。
- 循环辩论、Peer 回复、动态增加 Stage。
- Team 间聚合、比较或共同结论。

## 验收条件

- [x] 并行 Peer Capsule 的输入完全相同且不包含对方输出。
- [x] Research Manager 同时引用 Bull/Bear；Portfolio Manager 同时引用三个风险 Review。
- [x] 任一 Stage 失败或输出不符合 Schema 时只阻断当前 Team。
- [x] Publication gate 拒绝缺失 evidence、越界 stance、未来信息和数值 confidence。
- [x] Team Conclusion 不会创建或修改 `ledger_transactions`、`positions` 或订单形态数据。
- [x] 两个 Teams 的 Pipeline 配置与执行策略相同，差异只来自各自 Findings 集合。
- [x] 使用 fake Codex 的拓扑、并行和上下文泄漏测试全部通过。

## 可能触点

- `advisor/research/decision/contracts.py`
- `advisor/research/decision/pipeline.py`
- `config/research/pipelines/decision-v1.yaml`
- `config/research/stages/`
- `tests/advisor/research/test_decision_pipeline.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_decision_pipeline.py
```
