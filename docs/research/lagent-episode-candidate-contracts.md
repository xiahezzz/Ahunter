# Episode 候选阶段执行契约（开发 fixture）

`episode_candidates.py` 的 EpisodeCandidates 接住 EpisodeReplay 的阶段交接，使用
实际 GuardedCandidateRunner、CandidateGateway、PhaseCandidateProcesses 和
CandidateComputeCosts 执行 Test 封存的候选 Python 包。它没有队列、后台调度或
模型驱动，拥有者在同一 Test lease 下串行调用 step；真实模型与完整 LAgent 的
model/search/delegate 接线仍未完成。底层 ReplayProgram 当前只接受 fixture。

## 持久尝试与计费

首次使用封存主 actor、进程/通道/guardian 限制、恢复次数、产品授权描述、价格表
及候选 CPU 上界身份；每次 tick 重读当前配置并拒绝恢复时变更。产品 reader 的
生产 bundle 绑定、完整运行镜像和资源硬上界尚待验收，不能将这份描述视为全部
生产能力证明。

每阶段记录独立的尝试列表和 candidate_failures。先提交 prepared，再提交
dispatching 意图，之后才进入候选 CPU reserve、原子 budget.start/process claim
及物理启动。进程身份由 Test/phase/actor/主子/process_id 确定，不使用新的随机
身份掩盖未知的原执行。真实 guardian wait4 用量结算后才接受尝试结果；未知
清理/成本返回 cleanup_required 并保留费用占用，不发起下一尝试。

没有 process claim 的 dispatching 意图，在同 generation 的活动阶段返回
worker_recovery_required。新 lease fencing 旧 worker 后，或阶段权限已撤销后，
才能确证原执行不能再提交启动 claim；同时核对释放可能已经产生的 CPU 预留。
新尝试有新身份，属于平台中断恢复，不消耗候选自身的恢复次数。原 claim 已存在
则使用原 guardian 凭据核对，不盲重发；有明确退出回执的原执行也不会重复执行。
阶段恢复保留原快照和实际截止时间。

## 结果与停止

- 成功退出且没有必需数据/宿主失败，关闭当前研究阶段。
- 非零退出、候选协议/资源限制等候选失败消耗当前阶段的恢复额度。允许次数来自
  封存 runtime.candidate_recoveries；用尽后关闭当前阶段，继续后续预定阶段。
- guardian 最后一条传输消息丢失但已有验证过的持久退出回执时，沿用回执及宿主
  已提交的工具事实。保留 transport_code，不重复执行，也不据传输错误推造零收益。
- 研究费用不足/已禁止新增研究时关闭为 budget_exhausted，由 EpisodeReplay
  继续原终点回放；这没有补齐环境和评估的资源计量。
- 用户取消回调先持久化 Episode stop_reason，再通知正在执行的进程，防止后续
  tick 遗失一次性取消信号。关闭权限、回收进程、结算已知 CPU 后才能结束。
- 必需数据/能力缺失停为 blocked，明确宿主失败停为 failed；候选错误不冒充
  平台失败。阶段实际超时不延长截止，不消耗候选自身恢复次数。

## 尝试范围内的工具幂等

CandidateGateway 新增宿主可选 attempt_id，不向候选暴露或允许候选设置。未提供
时保留既有工具身份；提供时，同一尝试的重复动作仍幂等，新恢复尝试的同名
load_memory/save_memory/query 能读取最新已提交记忆和查询状态。主 actor 不变，
子权限仍只读；已有阶段计划继续依赖 plan_id 幂等，恢复不会重复冻结或多接计划。

query 原文/结果仍由 HistoricalQueries 保存，只额外记录状态、code、响应哈希，
不复制 items。必需产品确实缺失/质量失败，即使候选忽略错误并退出 0，也会阻断
Episode；可选产品不可用及候选越界请求不当作平台数据缺失。无交易入口的研究
阶段收到计划直接 plan_rejected，不先请求不存在的成交时点证据。

工具审计本身失败时，网关可能无法留下工具事件。PhaseCandidateProcesses 因此
把 platform_failure 另存为退出记录的 gateway_failure，并停止进程；不能仅以
退出 0 将必要审计失败算作候选成功。

## 验证与边界

新增测试使用真实本机 guardian/候选子进程，包含封存代码驱动 5 日买入/次日卖出
及 CPU 实际结算、每阶段首次失败后记忆恢复、不同恢复额度、意图/预留/退出后
worker 丢失、未知清理保留占用、取消真实进程、缺数据/审计失败/计划拒绝、原
截止超时以及最后传输消息丢失的持久回执核对。行情和价格仍为合成 fixture。

尚未完成共享 ResearchService 队列/心跳/Test 生命周期、真实模型/搜索/委派、
企业行动/特殊结算回放、环境/评估预算接线、完整资源/隔离能力、真实数据及正式
评分验收。formal_ready 继续为 false；不提供实验 API/CLI，不创建第二服务。

CLI/skill：现有 catalog 无变化，内部异步恢复/取消语义同步到 ahunter skill 和
workflows。此模块不能用作正式历史实验入口。
