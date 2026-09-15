---
id: LE-008
status: in_progress
phase: phase_1
depends_on: [LE-001, LE-002, LE-004, LE-005, LE-009]
---

# 候选隔离与单主多子研究运行

## 结果

封存候选可跨阶段持续研究和委托，无法访问外层答案、真实账户或未授权时间的数据。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D01, D07, D09）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-001](LE-001-specification-and-contracts.md), [LE-002](LE-002-records-artifacts-and-idempotency.md), [LE-004](LE-004-time-bounded-data-and-search.md), [LE-005](LE-005-clock-and-phase-snapshots.md), [LE-009](LE-009-cost-metering-and-calibration.md)

## 范围

- 复用现有 LAgent action/runtime 协议与 Research Service 执行基础，新增实验上下文；业务 finish 报告不直接变交易指令。
- 候选包只读挂载、依赖锁、独立 scratch、受控 env 数据与模型通道；不能修改平台或读取宿主生产数据库和其他 Episode。
- 实现一个逻辑主 agent 与配置化 child_concurrency，主/子共享预算/截点；子任务只研究，不 submit_plan 或扩大权限。
- 阶段间只恢复本 Episode 最后已提交记忆；运行中不改候选代码，不继承其他测试记忆。
- 模型固定 GPT-5.5 high 并记录实际标识、usage、超时及每次 attempt；不支持目标模型报错，不静默降档。
- 候选异常可证据归因、有限恢复；平台/服务错误单列；阶段结束 fence 并回收所有子进程和迟到结果。

## 验收条件

- [ ] 一主多子研究可运行，进程数变化不产生多个主决策者。
- [ ] 访问未来 bundle、隐藏 trace、原生搜索或宿主文件的尝试被阻断并留痕。
- [ ] 候选崩溃后下一预定阶段仍用同版本和已提交记忆，已接收交易计划不回滚。
- [ ] 模型不支持 seed 或费用能力时输出真实能力状态；不把 fixture executor 当真实模型。

## 可能触点

advisor/research/lagent.py 的复用 seam；advisor/research/experiments/runtime.py（新增）；advisor/research/codex/

## 验证

通过 run_phase/stop_phase 验证隔离、并发、恢复和迟到结果；离线假执行器用于稳定测试，真实模型在 LE-015。

## CLI / skill 影响

新增实验运行/失败类型、主子任务与模型使用字段需同步 skill；不得改变业务 LAgent 的实际交易边界。


## 2026-09-09 宿主工具网关增量

新增固定 PhaseSession 的 `CandidateGateway`，提供白名单 JSON 调用、固定错误码、
受限快照/账户视图、时间过滤查询、宿主补全规则费用的交易意图，以及同 Test/候选包
已提交记忆。主根会话才能提交计划和记忆；子会话即使复用主名称也没有写权限。
计划成功仅返回确认，提交后网关响应崩溃可恢复，不透传内部账户事件。

这只是宿主协议边界：尚未启动隔离候选进程、复用完整 LAgent 执行循环、接入模型与
共享预算、受控搜索/委托或阶段进程回收。LE-009 仍为集成前置条件，上述验收项尚未
完成。详见[网关契约](../../research/lagent-candidate-gateway-contracts.md)。


## 2026-09-09 宿主模型尝试控制增量

新增 `ModelInvocations`，复用 LAgentAction 输出结构，固定 Test/候选/模型/思考强度和
阶段会话。驱动器须提供单次执行、费用上界、非阻塞 poll/cancel/reconcile 与清理确认；
缺能力明确拒绝，既有 CodexExecutor 没有该适配，不能直接启用实验调用。

每个 attempt 独立预留/结算；预算 started 与分发 claim 同事务/CAS，崩溃或并发重试
不重复分发，恢复只核对身份。仅已结算 transport_error 能按配置重试；未知用量保留
预留。只有同阶段同 actor 的有效会话可取得合格 LAgentAction；迟到、错模型/强度、
格式错误或费用超额都不发布 action，已发生费用继续入账或保留待核对。

固定主 actor、子 actor 身份、在途并发、总子身份与逻辑步骤限额由封存配置控制。
PhaseClock.finish_close 等待模型尝试的清理确认；费用可在清理完成但仍未知时留待
后续核对。清理超时不伪造退出，cancel 操作要求驱动器幂等。

仍无真实候选进程隔离、完整主子任务/工具循环、生产驱动器或原生工具限制验收。
这是一层宿主控制，fixture 必须显式允许且 formal_ready 继续 false；不执行候选源码。
详见[模型调用契约](../../research/lagent-model-invocation-contracts.md)。

## 2026-09-09 本机隔离进程增量

新增 DarwinCandidateRunner：真实启动只读封存 Python 包，独立 scratch、最小 env、
关闭继承 FD，并使用 macOS default-deny 策略限制文件、网络和子进程。
配置化资源监测、TERM/KILL 回收和 wait4 用量读取已经用真实本地进程验证。

Apple 弃用的 SBPL 后端只用于内部开发验证；运行时镜像未封存，缺少硬 RSS/总磁盘
配额及崩溃恢复。CPU 信号、宿主采样限制不冒充已证明的硬费用上界。
尚未接入阶段/预算、双向 action/工具循环和持久清理审计，formal_ready 继续 false，
上述正式验收项不勾选。详见[进程边界契约](../../research/lagent-process-isolation-contracts.md)。

## 2026-09-09 阶段工具管道增量

PhaseCandidateProcesses 已将真实隔离候选接到 Test 固定网关。JSON 行协议有单请求、
帧/响应/累计/次数限额；子候选保持只读研究权限，查询结果保持时间过滤。独立进程
监控不因同步宿主查询而暂停，关闭/取消后丢弃待发送响应。

启动 claim 与候选进程投影先 CAS 提交，未知执行不盲重启；回收后持久保存状态与
用量，输出仅哈希。PhaseClock 同时等待候选/模型清理。宿主崩溃恢复及费用接线仍
缺失，原生模型/搜索/委托没有因此启用，正式验收项保持未完成。详见
[阶段工具管道契约](../../research/lagent-phase-process-contracts.md)。

## 2026-09-09 guardian 与 worker 丢失恢复

可选 GuardedCandidateRunner 由独立 guardian 拥有候选，worker SIGKILL 后仍能停止、
wait4 回收并持久写凭据。CandidateProcessRecovery 按当前 lease 原子关联凭据和清理
投影；停止标记/启动锁阻断迟到启动，有 intent 无凭据继续未知。真实故障用例已覆盖
新 owner 完成原阶段关闭。guardian 自身/整机故障、硬资源与计费仍待完成，formal_ready
继续 false。详见[guardian 恢复契约](../../research/lagent-guardian-recovery-contracts.md)。
