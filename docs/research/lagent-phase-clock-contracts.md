# LAgent 持久阶段时钟契约

2026-09-09：LE-005 内部实现中。已接通固定阶段、快照与持久查询权限，尚未形成
可运行的历史 Episode；本轮不调用模型、不创建模拟订单或账户账本。

## 阶段序列与时间

`PhaseClock` 读取 typed Test 的已封存定义，以解析日历生成初始研究及每交易日的
auction/postauction/postmarket 阶段。原五日任务共有 16 阶段；七日及显式休市 fixture
同样按完整日历展开，休市日不追加阶段。初始研究晚于首个交易窗口时，解析/时钟拒绝。

`Boundary` 同时保存事件界限与快照时刻。竞价后仍固定 09:25:00/09:25:05；
模型实际调用跨午夜、worker 重试或恢复不会推进模拟时间。实际激活时刻、经过秒数和
phase_wall_timeout_seconds 导出的绝对期限单独记录，恢复不重置期限。

控制器创建内部 clock，不代替 LE-010 的 create_episode/运行前置检查。只有持有
running Test 有效 worker lease 的宿主可提交阶段事实。

## 冻结与生命周期

- pending：下一预定阶段，尚未发放权限。
- active：已封存快照，允许受该阶段和 worker generation 限制的主子查询。
- closing：立即撤销权限；等待运行时完成进程关闭和结算的集成点。
- closed：宿主关闭完成后才允许 advance 到下一预定阶段；最后进入 clock finished。

快照输入由宿主提供，按相应产品的 Availability 和固定界限过滤。必需 item 或已声明
必需产品缺失，写入 quality blocked、进入 closing，不能推进或扩大时间窗口。
可选未来项不写入快照。快照的准备事实与 active 状态分开：字节/引用先持久化，
事件提交后才激活；崩溃留下的 prepared 快照不是 active 授权，重试仍复用同一内容。
快照准备和激活分别复核 lease/投影 revision，旧 worker 不能发布引用或激活阶段。

快照 item 引用内部不可变 JSON 产物；observe 校验哈希、时间与边界，再返回可见值。
外部 source-policy 原文不得通过快照转为永久产物，应走 LE-004 的保留期感知查询。

`PhaseClock.session` 创建与真实事件投影绑定的 PhaseSession，子会话继承权限。
查询提交事务内再次校验；closing、lease 失效、替代 worker 或期限到达均拒绝。
迟到回调由当前 owner 写 phase_late_result_discarded，仅保留输出哈希和回调身份，
不把迟到计划/记忆转入下一阶段。实际子进程终止仍需 LE-008 实现与验收。

## 恢复、停止与计分边界

阶段投影由不可变事件重建。动作重试返回原提交事实，不能追加第二次激活；同动作
不同输入冲突。worker 恢复只变更所有权，保留快照、阶段和期限；过期阶段不能靠
恢复续时。所有阶段权限同时受真实 Test worker lease 与持久 phase 状态约束。

candidate_error、候选恢复耗尽和 phase_timeout 关闭后可继续下一预定阶段。
cancelled/platform_failure 要求停止，不允许 advance。budget_exhausted 标记研究停止，
后续阶段可继续接受回放快照并关闭，但不再发放模型研究权限。实际预算触发与进程预算
仍需 LE-009/008。时钟不生成完成评分，也不把数据缺失当作零收益。

末日 valuation_trade_date 固定为任务最后交易日；末日盘后仍有阶段，但该模块从不
写账户/NAV。收盘估值与盘后研究费用的最终分离须由 LE-006/009/010 验证。

## 接收时序

`submission_timing` 接受宿主固定 PhaseDefinition 和当期 MarketRule，区分平台接收、
市场收到/接受与回执可用时间。市场未接受时等到下一允许区间，再加配置申报延迟；
越过区间末端则等待后续区间。没有有效接受窗口或生效规则时明确 blocked。

fixture 中 09:29 接收的竞价后计划在 09:30:00.100 才被允许接收；不回填开盘竞价。
默认申报和回执各 100ms 均从配置传入。取消在历史禁止撤单时段被拒绝；初始和盘后
阶段不接受计划。若竞价后截点与市场开盘竞价结束时刻不一致则拒绝相容性声明。
这里只计算模拟接收时刻，不生成订单、资金释放或成交，后者属于 LE-006/007。

## 验证与 CLI

24 项新增测试覆盖完整日历序列、跨午夜、双截点、必要证据晚到/未来/缺失、可见快照
值、主子 fencing、事务中关闭、固定期限恢复、投影重建、五个激活故障点、预算停止
后的全序列推进、取消/平台故障停止、迟到回调哈希审计以及各市场接受时间边界。

CLI/API 尚无实验运行入口，51 项 catalog 不变。ahunter skill/workflows 已同步内部
时钟能力和仍待集成的范围。原业务 Research、Market Daily 与 MX 流程保持原接口。
