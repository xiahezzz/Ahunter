# 隔离候选的阶段工具管道（LE-008 增量）

`PhaseCandidateProcesses` 把真实本机进程接到固定 CandidateGateway；所有 SQLite、
快照、查询、计划和记忆操作仍在宿主线程，候选只拿到 JSON 字节。底层隔离与资源
限制继承[本机进程契约](lagent-process-isolation-contracts.md)，formal_ready 仍为 false。

## 协议与执行

宿主从 gateway 所绑定 Test 的候选版本取 package_hash，调用方不能另外指定包。
runner 必须使用同一 ArtifactStore。主 actor 必须为指定主身份，子身份即使同名也
保持子权限。启动前校验活动阶段、lease、研究停止状态；进程 wall 上限取显式限制
与当前阶段剩余时间的较小值。

stdin 首行是 `{"protocol":"ahunter-candidate-tools@1","role":"main"}` 或 child。
候选 stdout 专用于请求，每行一个完整 ToolFrame：

```json
{"action_id":"observe-1","tool":"observe","arguments":{}}
```

stdin 下一行返回 CandidateGateway 的 JSON 响应。每次只允许一个未完成请求，
候选收到响应后才能继续发送；stderr 用于有界诊断。进程退出本身不表示研究成功、
计划提交或 Episode 完成。当前支持网关已有六项工具：observe、data_catalog、query、
submit_plan、save_memory、load_memory；没有原生搜索、shell、模型调用或 delegate。

ChannelLimits 必须显式提供 frame_bytes、response_bytes、total_response_bytes 和
max_requests；前两项包括行尾换行，累计响应限制不含初始协议行。它们尚未接入封存
实验资源包络，不以测试额度作为产品默认。输入必须是 UTF-8 JSON 对象，重复键、
NaN/Infinity、非法/未结束帧、超限和观察到的请求流水线均停止进程。stdout/stderr
仍共享底层总输出限额；导致超限的请求不会再交给网关。响应超限停止，不截断成一个
貌似完整的 JSON 结果。

独立线程监控真实进程的管道与 wall/scratch 限额，宿主线程执行同步网关调用。
即使查询适配器正在等待，监控仍能 TERM/KILL 候选。适配器返回后重新检查阶段和
进程状态，失效响应不送回候选；同步宿主适配器自身仍须有超时，本层不能强停其 Python
调用。阶段检查或用户取消失效会清空待发送队列并终止候选。已经提交的计划/记忆不
因候选退出或管道中断回滚；同 action_id 的恢复语义由现有网关负责。

## 持久性和清理

启动前以 Test/phase/actor/角色/process_id 派生执行身份，并 CAS 提交 dispatch_claimed
事件与 candidate_processes 投影。用于身份/并发判定的同一投影序号参与 CAS，重复或
并发请求不能借新读状态绕过判定。同 actor 不可有另一个未回收进程，子进程数受封存
subagent_concurrency 限制。完整子任务队列/累计额度仍待主子调度器集成。
claim 提交后、线程尚未启动前再次检查阶段；已关闭时记录确定的 not_started 并
抑制执行。它不同于重启后无法确认的历史 claim，不需要假设某个 PID 已经退出。

进程结果只有在底层 wait4 回收后才写 reaped/quiescent，包含退出状态、原因、PID、
策略哈希和进程 CPU/RSS 用量。stdout/stderr 只持久保存哈希；工具请求的幂等、权限
拒绝与过滤结果沿用网关原有记录，不把进程输出当额外的授权或可信报告。

PhaseClock.finish_close 现在同时等待模型调用和候选进程清理。启动提交后崩溃、监控
失败、旧 worker lease 失效导致无法记清理，都保留未确认状态。相同执行身份即使
看起来已结束也不重新启动，不通过 PID 存在/不存在猜测安全回收。直接后端的未知
清理会阻止阶段完成，不能手工把 quiescent 改为 true。后续可选 guardian 后端已提供
worker 丢失后的回收凭据和当前 owner 核对，确切范围及 guardian 自身丢失的缺口见
[guardian 恢复契约](lagent-guardian-recovery-contracts.md)。

## 验证与仍未完成的工作

真实进程测试覆盖主候选观察/记忆/计划、同名子权限、未来产品过滤、关闭期间等待
真实回收、慢宿主查询不暂停进程 deadline、格式/重复键/未结束帧、请求/响应/输出
限额和宿主异常；事务故障用例验证不确定启动不能盲重发。使用临时数据库与样例证据，
不调用真实模型、搜索或真实账户。

生产模型驱动、delegate/action 主循环、预算预留与计算费用落账、心跳/崩溃恢复、
正式运行时镜像与完整安全验收仍待完成。这个阶段工具适配器不新增 API/CLI，不能
作为 LE-010 Episode、LE-014 全面对抗或 LE-015 真实运行的完成证据。
