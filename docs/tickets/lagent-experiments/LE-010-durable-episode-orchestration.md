---
id: LE-010
status: in_progress
phase: phase_1
depends_on: [LE-002, LE-005, LE-006, LE-007, LE-008, LE-009]
---

# 持久 Episode 编排、取消与恢复

## 结果

单次历史任务跨所有预定日和阶段完整执行，服务重启、预算耗尽及候选缺陷不会产生重复副作用。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D01, D03, D09）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-002](LE-002-records-artifacts-and-idempotency.md), [LE-005](LE-005-clock-and-phase-snapshots.md), [LE-006](LE-006-account-fees-and-valuation.md), [LE-007](LE-007-execution-and-replacement.md), [LE-008](LE-008-isolated-lagent-runtime.md), [LE-009](LE-009-cost-metering-and-calibration.md)

## 范围

- experiment 对接现有 durable queue/Research Service，以 Test Record 安排整个 Episode；不按日新建相互独立账户或第二调度器。
- 实现 preflight/queued/running/evaluating 及终态，阶段 active/closing/closed 与结束原因分别记录。
- 用 lease/fencing、持久安全点、outbox 驱动模型和 env；旧 worker 不可回写，未知外部调用先核对不盲重发。
- 区分普通计划错误、候选崩溃、服务故障、数据缺失、预算耗尽及用户取消；按定稿决定继续/停止和可计分性。
- 阶段研究额度耗尽仍推进已接受计划/撤换至原终点；用户取消终止回放且不出正式评分。
- 同次恢复追加 attempt，终态重新运行新建 linked record；运行与成本对齐，必要审计失败不继续新副作用。

## 验收条件

- [ ] 服务在计划接受/冻结/成交/撤单释放/结算边界重启，账户无重复且阶段不跳过。
- [ ] 完整 5 日及非 5 日 fixture Episode 跨日状态正确；09:30 至 23:00 没有模型调用。
- [ ] 候选自身异常合法保留低收益，平台/数据异常不假作零收益；预算耗尽与用户取消路径不同。
- [ ] 队列操作不启动第二服务、不影响业务研究账户，迟到 worker 不能修改已完成记录。

## 可能触点

advisor/research/experiments/service.py（新增）；现有 Research Service/queue；experiment preflight.py

## 验证

共享队列与完整 Episode 的故障注入集成验证，不执行真实行情收益推断。

## CLI / skill 影响

异步提交/取消、终态、恢复与 rerun 明确映射 CLI；skill 说明 HTTP 成功不代表 Episode 完成。

## 2026-09-09 候选清理恢复增量

CandidateProcessRecovery 已接回原 Test 的 guardian 执行凭据：当前 lease 可在 closing
阶段核对已回收或确定未启动的候选，关联记录/artifact/事件/投影同事务提交；旧 lease
不可写，未知执行不重发。真实 worker SIGKILL、新 owner 核对后关闭阶段及提交故障
已验证。详见[guardian 恢复契约](../../research/lagent-guardian-recovery-contracts.md)。

这仅是 Episode 恢复的一个组成部分；完整队列接线、心跳、跨日回放、预算耗尽、
评分资格及全部成交/结算故障边界尚待实现，以上验收项不勾选。

后续计费增量：候选 CPU 的预算 started 与进程 claim 现可同事务提交，清理和计费
分别有可核对身份。CandidateProcessRecovery 可继续收取已完成清理但未到账的用量；
终态核对只追加原预算之上的事实。资源范围/硬上界仍未齐全，详见
[候选计费契约](../../research/lagent-candidate-compute-contracts.md)。

## 2026-09-09 封存 fixture 回放增量

EpisodeReplay 已串起单 Test 的阶段时钟、计划推进、分钟/队列、DAY 失效、应付款
结算与 NAV。程序先封存；动态命令先持久化，再调用原领域幂等 API，最后推进游标。
5 日与 3 日 fixture、实际买入/次日卖出、费用及每日估值、领域提交后新 lease 恢复、
收盘队列/DAY 释放、研究恢复交接及预算/取消/失败分流共 22 项测试通过。
详见 [Episode 回放契约](../../research/lagent-episode-replay-contracts.md)。

该实现仅接受合成短行情 fixture，研究由测试宿主交接；没有完成全天真实数据覆盖、
完整 LAgent 执行、企业行动/特殊结算回放、环境预算、共享队列及正式评估。以上验收
项继续保持未勾选，ready_for_evaluation 不等于 Test completed 或可计分。

## 2026-09-09 候选阶段执行增量

EpisodeCandidates 已接入实际 guardian 候选进程和候选 CPU 预算，持久化每阶段
尝试/dispatch 意图/结果，并按原配置分别执行候选恢复次数。恢复尝试具有新的
宿主工具身份，可读取最新已提交记忆；已有 plan_id 保持幂等。worker 丢失先核对
原进程/预算，未知清理不重发。取消意图先落盘，进程回收/已知费用结算后才结束。
详见 [候选阶段执行契约](../../research/lagent-episode-candidate-contracts.md)。

这仍未完成生产模型/搜索/委派、共享队列/心跳/Test 生命周期和环境预算，LE-010
完整验收项保持未勾选。

## 2026-09-09 共享 ResearchService 接入增量

业务 Request 与实验 Test 现使用共用调度索引和原 ResearchService 单例/心跳。
Test 不写入虚构业务请求；入队、认领、换主、取消和 evaluating 等待均有持久
绑定，旧 lease 同时受服务 epoch 约束。混合排队、真实候选 5 日执行、成交提交后
重建服务、续租、取消和状态 API 共 13 项新增验证通过，详见
[共享服务契约](../../research/lagent-shared-service-contracts.md)。

当前只有内部 fixture admission/factory，默认生产服务未启用实验执行入口。
评估器与完整模型/环境预算/真实数据仍未齐全，evaluating/waiting 不冒充 completed，
完整验收项不勾选。

## 2026-09-09 企业行动与特殊结算接线

EpisodeReplay 现封存并执行五类企业行动/特殊结算点。预定周期内每次生效/到账
必须恰好出现一次且使用原固定时点；权益登记排在同刻市场事件之后和 checkpoint
之前。周期外到账保留应收，不延长任务。原 CorporateActions/SpecialSettlements
执行器负责税款、股份可卖及完整持仓分配匹配，领域提交和游标分离恢复不重复入账。

新增 35 项测试，与原 Episode/公司行为/特殊结算共 111 项通过，覆盖多日金融状态、
五类提交后换主重建、取消、组合税务和缺证据明确阻断。详见
[Episode 契约](../../research/lagent-episode-replay-contracts.md)。正式来源完整性、
任意实际持仓的条款分配适配器、环境费用/模型执行及正式评估仍待接入，完整验收项
保持未勾选。
