# 宿主模型调用与清理契约

`ModelInvocations` 管理宿主模型尝试，不加载或执行候选源码。候选进程隔离、完整
主子研究循环及真实驱动器仍未实现；本接口不能使实验 preflight 或 formal_ready
变为可运行。既有业务 LAgent 协议以 LAgentAction 作为结构化输出继续复用。

## 驱动与身份

驱动器提供封存 DriverCapability，以及 quote/start/poll/cancel/reconcile 方法。
quote 必须是无模型副作用的费用上界声明；start 每次仅分发一个外部尝试；其余
方法须非阻塞，并在全部受控进程退出后才返回 quiescent。生产驱动应执行收到的
原期限和 cancel_wait_seconds。当前仅 fixture 验证合同；旧 CodexExecutor 没有
可靠费用上界适配，明确 cost_capability_missing。不能用其内部重试冒充独立预算。

Test、候选包、主 actor、阶段、actor 是否子任务、模型和思考强度均固定。逻辑
call_id 跨传输重试保持同输入，attempt 由配置 retries 限制；全局 invocation 身份
包含 Test/阶段/actor/角色/逻辑调用/attempt。主 actor 固定，子 actor 即使同名也
不能冒充主。每 actor 同时一个调用，子在途并发、全 Test 的子阶段身份总数及逻辑
调用步数读取封存设置；这些调用限制不代替后续完整委托任务登记与排队。

## 预算与分发

准备记录固定输入/候选/输出 schema/期限与上界，证据永久引用。之后预留，再把
预算 started 和唯一分发 claim 在同一事件/CAS/租约事务提交。额外投影的哈希
包含每次竞争者的 claim；并发恢复不会都拿幂等 started 回执执行一次外部调用。
提交后、真正分发前崩溃仍是需要核对的状态，不能假设免费或盲重试。
真正调用驱动器前再检查阶段和期限；已关闭则抑制分发，保留预留至明确零用量回执
完成核对。生产驱动器仍负责取消信号与进程创建边界的可靠执行。

driver start 异常、用量缺失或模型实际标识不符保留预留和未核对状态。只有前一个
已结算的 transport_error 可以使用下一次配置重试，费用另计；成功、未知和
candidate_schema_invalid 都不属于运输重试资格。token_cap=null 保留其语义。

## 结果与清理

完成回执须先证明 quiescent，再按同一预算回执规则结算。实际模型/强度必须匹配，
费用超额、无效 LAgentAction 或迟到结果不发布 action。原始无效/迟到输出仅保留
哈希；符合结构的公开 action 也仅限同阶段同 actor 的当前会话读取。下游还必须
验证产品权限、证据及操作授权，不能把 finish 报告转换为交易计划。

最终 action 提交事务内再次检查会话和期限，避免结算过程中阶段关闭的竞争。
stop_phase 请求取消并轮询确认；PhaseClock.finish_close 在准备中的调用未释放
或活动调用未清理时拒绝。清理期限耗尽仍保持未确认状态，不伪造进程终止。
费用未知而进程已停止时，可以结束阶段并保留预留，随后 reconcile 只核对费用，
输出不能流入下一阶段。Test 进入终态后的费用走 TerminalCosts；此运行时不重开 Test。

真实驱动仍须实现进程树终止、外部任务身份恢复、工具与网络限制及可靠成本能力。
完整候选运行/工具派发、受控搜索和计算成本仍待集成，没有新增 API 或 CLI。
