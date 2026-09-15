# 候选 guardian 与 worker 崩溃恢复（LE-008/010 增量）

`GuardedCandidateRunner` 将候选进程的所有权移到独立 guardian 进程。
worker 的工具网关/SQLite 操作仍在原线程；guardian 只操作封存包、受限候选、私有
IPC 和清理证据。worker 退出或失去连接后，guardian 继续 TERM/KILL 并 wait4 回收
候选。`CandidateProcessRecovery` 让当前 lease 的 worker 将清理证据原子写回 Test。

这已经通过真实 worker SIGKILL 验证，但不是所有宿主故障的完成声明：guardian 自身
或整机故障、硬资源包络、计算计费与完整 Episode 编排仍未完成。formal_ready=false。
底层沙箱的[平台/资源限制](lagent-process-isolation-contracts.md)仍然适用。

## 准备、分发和 IPC

PhaseCandidateProcesses 可选用 GuardedCandidateRunner。它在提交 Test 的 dispatch
claim 前准备私有执行目录，将包、输入摘要、ProcessLimits、GuardianLimits、标准库/
本地依赖路径和 ArtifactStore 位置绑定到执行 identity、随机 nonce 与 request_hash。
请求文件只写一次，相同身份的不同输入拒绝。候选输入正文不写入该请求文件。

DB claim 保存 guardian handle。分发先独占写入 launch 标记，再启动 guardian；
重复分发不启动第二个 guardian。guardian 启动后持有文件锁，在检查停止标记后才
允许产生 child-intent。intent 在创建候选前落盘，child-started 在 Popen 返回后记录
候选/guardian PID。PID 是诊断字段，恢复不会据此给未知进程发信号。

worker 与 guardian 使用私有 socketpair，帧和待发送总字节均显式有界；运行前检查
配置的 IPC 帧能容纳进程输出凭据。guardian 通过单独解释器启动，只接收指定的
IPC FD 和最小环境。候选的 close_fds/空 pass_fds 边界保持不变，不继承这个 socket、
日志锁或 ArtifactStore FD。guardian 私有目录不在候选读取/写入白名单中。

候选 stdout 经封装转发给 worker 的 ToolChannel，响应和实际发送确认按序返回；
候选不能把 stdout 伪装为 guardian 的控制消息。已有网关时间/角色限制继续生效。
IPC 断开、owner heartbeat 超时、不可撤销停止标记或工具管道失败都触发候选停止。
GuardianLimits 的 ipc_frame_bytes、ipc_pending_bytes、owner_timeout_seconds 必须
显式指定，尚未成为实验 preset 或硬成本包络；guardian/worker 自身开销尚未计费。

## 清理凭据

guardian 仅在底层 runner 完成真实 wait4 及候选临时文件清理后发布 reaped 凭据，
包含固定执行身份、请求哈希、候选包、退出状态/原因、进程 CPU/RSS 和 stdout/stderr
哈希。原始进程输出只经有界 IPC 返回，不写入永久清理日志。正常路径要求 IPC 结果
与持久凭据完全相符，并等待 guardian 退出后才从 run 返回。

文件发布使用私有暂存文件、文件 fsync、独占硬链接与目录 fsync；已有文件不能被
不同内容覆盖。读取拒绝软链接、非普通文件和非 canonical JSON；验证 nonce、请求/
包绑定、状态、输出哈希及非负有限用量。not_started 凭据不能与 child-intent 共存。
这些是可信宿主私有目录中的执行证据，不声称抵抗拥有宿主文件写权限的攻击者。

活跃 worker 将凭据复制到 ArtifactStore，关联 invocation 记录、artifact references
与 candidate_processes 投影在同一事件事务提交。IPC 结果丢失但凭据完整时，可以
直接记录已知清理结果；没有凭据则保持 cleanup_unknown，不伪造零费用或退出状态。

## 当前 owner 的恢复

`CandidateProcessRecovery(records, lease, runner).reconcile(identity)` 不需要一个活动
研究会话，因此可在原阶段 closing 时调用，但必须持有当前 Test lease。它核对原
Test claim/guardian handle/包/限制，并采用以下规则：

1. 先持久写入不可撤销的 cancel 标记。未来迟到的 guardian 必须在同一启动锁内看到
   它并停止；已运行的 guardian 监控该标记并回收候选。
2. 已有合法 reaped 凭据时，采用原执行结果；不重启、不重新执行计划或查询。
3. 没有凭据但能独占取得锁，且没有 child-intent 时，可发布 not_started：已有
   guardian 不在启动临界区，未来 guardian 又已被停止标记禁止启动。
4. 锁仍被占用，或已有 child-intent 却没有合法回收凭据，返回 pending。锁空闲、
   PID 不存在、经过很久都不是已回收的证明。

恢复将凭据 artifact、关联记录、事件和清理投影同事务提交；提交前失败不解除阶段
等待，提交后响应丢失可幂等读取。旧 lease 不可写。PhaseClock.finish_close 继续等
候选与模型清理，不因出现 journal 文件或 worker 已退出就直接放行。

guardian 自身在 intent 之后被杀可能留下候选；当前没有该双重故障的进一步回收
证明。机器重启、运行时镜像封存、全平台隔离、失去 lease 的服务心跳与完整队列恢复
也尚未验收。这里的 reaped 证明覆盖候选；恢复时 guardian 自身的后续清理开销尚未
纳入资源账本。直接使用 DarwinCandidateRunner 的旧执行没有 guardian handle，
不能通过本恢复接口补造凭据。

## 验证与接口影响

真实测试用临时数据库启动独立 worker 和 guardian，强制杀掉正在运行的 worker，
确认 guardian 回收候选、原阶段仍等待 DB 清理事实，然后由新 lease 提交凭据并关闭
阶段。另覆盖普通执行/双向工具、迟到 Popen、未知 intent、句柄错配、用量篡改、
候选读取/修改 journal 与信号/FD 越界、恢复提交前后故障和重复核对。

这些测试不操作真实 API worker、模型、搜索、生产数据或真实账本。没有新增实验
API/CLI；catalog 保持现有 51 项，skill/workflows 同步能力与剩余缺口。
