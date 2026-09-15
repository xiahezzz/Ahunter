# 候选 CPU 计量与费用接线（LE-008/009/010 增量）

`CandidateComputeCosts` 将 guardian 的真实候选 CPU 凭据接到现有 CostBudget。
已实现研究桶预留、与进程分发原子开始、清理后计价、未知用量保留、当前 owner 恢复
及终态费用核对。它仅覆盖候选进程 CPU，resource_scope_complete 和 formal_ready
始终 false，不代表完整 Episode 资源或硬费用能力已通过。

## 计量范围与启动门槛

价格表必须包含单独的 compute 资源，唯一 meter 为 `seconds / cpu_second`，其
usage_semantics_hash 必须指向 `CPU_SEMANTICS`：guardian 对该候选 wait4 返回的
user_cpu_seconds 与 system_cpu_seconds 之和。worker、guardian 自身、内存、I/O、
环境回放和评估全部明确排除；应使用各自的资源/计量语义，不能复用本范围假装计全。
求和和价格相乘采用现有精确十进制运算，不把二进制 float 运算误差累计进费用。

当前 macOS 后端不能证明物理 CPU/完整费用上界，因此本模块没有可启用的正式启动
能力。默认 prepare 返回 compute_cost_capability_missing。只有调用方显式传入
fixture_maximum_cpu_seconds 且价格表 evidence_kind 为 fixture，才能运行开发验证。
这个数是样例上界，不是从 SIGXCPU 或 wall 监测推导出的硬能力；真实进程用量仍由
guardian 测量，超出样例上界也按实收费并记录失败。不能把这种验证提升为真实标定。

## 预留与分发

PhaseCandidateProcesses 可选注入 compute。它先准备 guardian，再在研究桶 reserve。
绑定记录、来源 artifact 与预留同事务保存：包含 Test、阶段、actor/主子身份、包、
guardian handle、进程限制、定价/计量语义和完整 CallBound。环境与评估预留不可借用。
能力缺失或预算拒绝都不启动候选。

CostBudget.start 与 candidate_processes 的 dispatch claim 同一事务/CAS；claim
具有唯一分发标识，避免并发者把幂等 start 回应误当成新的执行许可。物理 guardian
启动还有已有的独占 launch 标记。启动结果不确定时不会重发。主/同名子和不同执行
各有全局费用身份，同一执行的重复核对不产生新账单。

## 凭据、结算和恢复

`collect(identity, runner)` 是会执行停止核对和结算的宿主操作，不是纯状态查询；
日常费用读取仍使用 CostBudget.read。collect 验证原绑定、价格、guardian 请求与
执行限制，然后调用 guardian.reconcile；这会持久停止原 guardian，绝不会启动它。

- 仅 reserved 且 guardian 证明从未启动候选时，释放预留。预留提交后、进程 claim
  前崩溃也能通过永久绑定记录按原 identity 找回。
- 预算已 started，但 guardian 证明物理未分发时，结算候选 CPU 为零，而不是把
  started 改回未开始。这不表示 guardian/worker 免费；供应商账单仍为 null。
- 已回收候选按 wait4 实际 CPU 计价，成功、失败、取消均保留已发生用量。实际超额
  继续收费，标记 cost_upper_bound_exceeded，后续研究预留拒绝。
- 有 intent 无清理凭据时，不推断零用量，started 标成 unsettled 并保留原上界。

每次确定结算保存 measurement 记录，关联原 binding、guardian receipt artifact 和
标准 UsageReceipt。来源记录与费用结算/释放同事务提交。现有 CostBudget 与
TerminalCosts 的内部 guard 接口用于组合这些写入，原验证、lease 和幂等规则保持。
恢复核对已完成条目时仍验证 measurement 与账本回执相符、来源 artifact 完整。

PhaseCandidateProcesses 正常退出/抑制未启动路径会调用 collect；CandidateProcessRecovery
可注入当前 lease 的 compute，即使清理投影已经完成，也继续核对未到账的用量。
费用未知可保留，不能为了关闭阶段伪造结算。失去 lease 的旧 owner 不可写活动 Test。

终态 collect 通过 TerminalCosts 追加原预算之上的确定核对事实，measurement 与该
事实同事务；原 Test、原成本投影和账户不重开、不改写。重复核对幂等。来源缺失、
句柄/语义不匹配或无启动授权的已执行候选都失败关闭，不补造收费资格。

## 验证与剩余范围

测试用独立候选 CPU 样例 tariff 保持环境回放资源语义分离，实际运行封存候选并从
guardian 取用量。覆盖预算保护、拒绝不分发、真实超额、同名主子独立费用、预留/开始
崩溃、未分发与未知的区别、活动/终态结算事务故障、新 lease 及重复核对。

完整 worker/guardian/内存/I/O/回放/评估测量、硬上界验证、资源包络和真实价格表仍
待完成；本组件不能单独证明全实验费用齐全。没有调用模型、搜索或真实账户，也没有
新增 API/CLI，catalog 保持 51 项；skill/workflows 已同步内部能力和剩余门槛。
