# 候选宿主工具网关契约

`advisor/research/experiments/gateway.py` 提供内部 JSON 分发器。它固定绑定宿主创建的
PhaseSession、Test 和封存候选包；候选只交换 JSON，不能持有此 Python 对象或其
records、account、provider 回调。当前尚无隔离进程或真实模型执行入口。

## 输入与输出

`{action_id, tool, arguments}` 是唯一帧结构。白名单为 observe、data_catalog、query、
submit_plan、save_memory、load_memory；未知字段/工具和非法 JSON 拒绝。异常文本不
返回给候选，拒绝审计保存请求哈希，避免把请求中的宿主路径或秘密写入错误事件。

observe 返回固定阶段边界、已批准快照内容及该边界可见的账户余额/持仓/应付款；不
返回最新 execution、完整历史队列、隐藏评估或宿主账户事件。catalog 仅返回授权产品；
query 复用 HistoricalQueries 的日期与可用时间过滤，持久化只使用其已过滤审计结果，
不另存一份原始来源内容。相同查询身份重试复用原结果，禁止访问产品的拒绝也稳定复用。
同一 action_id 不能换工具或请求内容。阶段关闭后数据读取拒绝。

submit_plan 只接受纯新单意图（证券、方向、数量、限价、订单 ID）、取消或替换。
宿主回调补全来源、历史规则和费用，StagePlans 在事务中重新验证作用域/截止。
成功仅返回 accepted 与 plan_id。业务提交后、响应前崩溃可从已接受计划恢复确认，
即使阶段已关闭也不重复解析市场上下文或重复冻结资源；不同内容复用身份则拒绝。

只有指定主根会话能提交计划或 save_memory；子会话复用主名称仍无写权限。
记忆是 summary、decisions、next_questions 的显式工作摘要，不是私有思维链。
记忆投影和确认事件同事务提交，只恢复同 Test 与同封存包的最后已提交版本；回滚
不覆盖上一版本，版本/候选包不一致拒绝。跨阶段不会继承另一 Test 的记忆。

## 未完成的集成边界

此分发器的对象隔离约定不能代替 OS 隔离。候选只读包挂载、独立 scratch、宿主文件与
网络访问控制、现有 LAgent action/run_phase 协议适配、共享预算/成本/调用限制、
真实模型能力、委托/受控搜索和阶段子进程回收仍待 LE-008/009/010/015。
child() 只创建宿主授权会话，不启动模型或授予候选自行创建无限子任务的能力。
没有新增实验 API/CLI，也不能据此宣布可运行或可正式评分的 Episode。
