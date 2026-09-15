# ResearchService 共享队列与 Test 生命周期

`research_work_queue` 是业务 Research Request 和历史实验 Test 共用的调度索引。
两类任务保留各自的领域记录，Test 不伪装成团队/标的请求。ResearchService 的
同一单例租约、同一 tick 和同一心跳线程负责认领；没有新增服务或独立实验调度器。

## 队列与迁移

业务 Request 插入/更新/删除时，SQLite trigger 同事务同步调度索引。现有请求由
幂等迁移回填，后续认领按同一索引的队头执行：运行中的任务优先，随后沿用手工
优先于 scheduled、接受时间、ID 的排序。业务排队取消的既有资格规则继续适用。
同一个 Service 实例的并发/重入 tick 被非阻塞锁拒绝，避免一次执行尚未结束又
认领另一任务。

ExperimentWorkQueue.enqueue_fixture 只接受首次 worker 认领前、已 queued 的
typed Test 和显式 fixture descriptor。descriptor 以不可变 artifact 和 service_input
记录保存，并与入队行同事务提交；重复输入幂等，换输入冲突。它不代替真实能力
preflight，也没有公开实验提交命令。默认生产 service 尚未配置实验 runner factory；
已有内部 fixture 工具仍能操作未入队 Test，一旦入队就必须遵守共享服务所有权。

调度索引缺失时，已 service_input 绑定的 Test 拒绝退回独立 lease 权限。迁移可
从永久入队记录、Test 状态事件和取消事实恢复丢失索引，非终态重新排队核对；
不会改写原账户、阶段、Test 状态或已存在的活动认领。

## 所有权与心跳

Test worker claim 与调度行的服务 owner/claim owner 在同一事务更新，队头、
输入和当前单例租约都须再次匹配。正常重复 tick 复用仍然有效的本 worker lease；
服务换主先撤销旧调度认领，保留原 Test running/evaluating 状态，再认领新的
Test generation。即使旧 Test lease 尚未过期，也不能继续写：每次实验写入同时
验证调度行和当前 ResearchService epoch。业务请求的已有 claim 检查也核对已绑定
的 Service epoch，换主与请求恢复之间不留可写窗口。

执行期间，现有 Service 的独立 SQLite peer 事务同时续租 Service 和 Test，采用
两者更短的心跳间隔，Test TTL/heartbeat 来自封存 runtime 配置。慢的宿主数据
读取不阻断续租；丢失所有权则停止原执行并禁止原 worker 终态写入。新 owner 仍
通过原 guardian/预算凭据核对，不根据 PID 不存在推断清理已完成。

## 调度、取消与评估等待

可选 experiment_runner_factory 只构造绑定该 Test/lease/records 的 EpisodeCandidates，
不能在构造阶段自行执行外部调用。Service 将 queued 推至 running，驱动候选和
回放 tick。cleanup_required/worker_recovery_required 保持原工作未终结，CLI 的
服务循环按既有 poll_seconds 等待，不紧密空转或越过未知清理认领新任务。

取消作为永久 service_control 事实与队列标志同事务记录。首次执行前取消不创建
候选、预算或账户；运行中取消交给 EpisodeCandidates 持久停止并完成必要清理。
已确认 Episode cancelled/blocked/failed 才映射 Test 相应终态。构造失败若尚有
未知进程则保留核对责任；不产生伪造完成或零费用。

回放 ready_for_evaluation 后，Test 进入 evaluating。只有完整 Episode、finished
阶段时钟及物理清理都已确认，才能将调度行置为 waiting，释放执行队列供后续
任务使用。当前评估器尚未实现，waiting 不等于 completed 或可计分；没有执行
自动评分。取消 waiting 会重新排入服务，保留取消事实后终结 Test。未来评估
接入还须明确唤醒、评估资源预算和正式资格审查。

## 状态、验证与运行边界

现有 `GET /api/research/requests/current` 的 service 增加 nullable active_test_id；
active_request_id 继续只表示业务请求，queued_count 统计共用索引。requests 数组
仍只包含业务 Request。服务 CLI 状态同步这个字段；未新增 API 路由或命令。

13 项新增测试覆盖混合队列与真实候选驱动的 5 日买卖、提前取消、评估等待取消、
服务换主及仍存活的 Test lease、队头/CAS 回滚、未知清理占用、独立 peer 在真实
候选等待时续租、迁移回填/索引修复、重入防护、业务 epoch fencing、现有状态 API，
以及成交提交后、游标提交前重建服务并继续同一 Episode。

测试使用合成行情/价格和本机 guardian 候选。生产模型/搜索/委派、环境/评估费用、
企业行动/特殊结算回放、评估器和真实数据仍未齐全；formal_ready=false。没有启动
真实历史实验、模型调用或新增服务。LE-010 完整验收继续保持未勾选。

部署是附加表、索引、trigger 与幂等回填。旧数据库应先运行既有 schema migration，
然后重载已安装的 API/ResearchService；不启动第二个临时服务，不中断活动业务
请求。CLI catalog 不变；skill/workflows 同步状态字段及取消/恢复语义。
