# LAgent 初始基线、最终选择冻结与留出登记

实现：`advisor/research/experiments/selection.py`，复用 typed registration 和原
selection/exposure 永久记录。当前是内部控制接口，不新增服务、数据库或 API/CLI。

## 初始基线及冻结

`ExperimentSelection.initialize` 将预先登记的根候选及其定义封存为初始/当前基线。
须在任何 Test 开始或建立预算前完成；重复调用只返回原记录，不能换根或重置已冻结
的选择。定义和候选包按现有哈希、artifact 验证规则读取。

`finalize` 比较 expected_selection_id 与数据库当前记录，在同一写事务中保留前序
选择 ID/哈希、初始及当前候选包、日期 exposure 快照。所有计划样本须已终止，且
候选/模型物理清理已确认；尚未执行的 created 样本同样不能被默默略过。两个连接
竞争冻结只有一个成功，过期 revision 不能再冻结。幂等重试保留原快照。

冻结后，同实验不能登记新候选、新调优/选择/标定计划或调优 rerun；此前已成功
登记的原输入重试仍有效。新定义可以作为永久草案保留，但不能替换该次冻结定义。
这不会修改运行中候选、业务配置或其他实验。

`apply_comparison` 消费完成的原始比较，在写事务中重新核对原始证据与判定。比较
必须预先绑定 expected_selection_id、当前对手及原定义，并取得匹配运行条件的
正式宿主评估资格。合格比较只给 eligible，直到此操作实际提交 promoted 才移动
当前选择。初始基线 ID/包与前序选择保持不变，新增当前候选及 Comparison 关联。

每个比较只保留一次选择决定。等分、低收益、不稳定、不完整资格均不改指针；旧
revision 记 stale_baseline，不能把旧比较重新绑定到较新选择。对照父版本获胜也
不能替代与当前基线的新比较。已最终冻结时记 selection_frozen。非晋级决定不会
成为新的选择 revision，避免无效比较使其他合格比较过期。重复提交返回原决定。

两连接竞争同一 revision 只有一方 promoted，另一方保留 stale_baseline；晋级后
最终留出使用初始和实际最终候选。正向路径以合成的已验收宿主评估契约测试，真实
Episode 仍缺正式资格/完整计费生产路径，因此现有 fixture 不会据此晋级。冻结只
固定测试对象，不把合成合同测试当作真实研究成绩。

## 一组预定留出测试

`ExperimentRegistry.test_plan(..., purpose="final_holdout", selection_id=...)` 必须
引用同实验当前 finalized 记录及原定义。只允许初始基线和最终选定候选，覆盖所有
预先登记的 final_holdout 任务、全部重复、seed 计划与交错顺序。若初始与最终仍
为同一个 proposal，只运行一份完整重复集合，不重复制造相同版本的样本。

整个 plan 与全部 Test、selection links 原子登记。每次冻结只允许一组计划；换
plan_id 不能增加重复，final-holdout Test 不能 rerun。输入重试不会新增样本。
留出继续只做 holdout 诊断，现有 ComparisonAssessments 不接受它作为晋级计划。

## 日期暴露与审计

检查实际留出交易日期，不依赖 task_id、实验名称、定义 ID 或角色改名。在当前
定义中与 tuning/selection 交易日重叠的日期不能称为留出。单人项目的历史 exposure
跨实验读取，含外部声明的日期区间和已读评估/事件/数据的 scopes。旧非留出任务
曾进入 running 的日期也保留为训练使用证据，即便该任务后来失败或没有独立的
owner 隐藏读取审计；调优信息本来就可以直接读取。

未知日期的 exposure 保留 unresolved，不猜测它不影响本次窗口。历史记录不可
修改或删除来抹除已知日期。无交集的旧 exposure 不污染新窗口。

查看 typed definition/TestPlan/Test/selection 的登记元数据仍保留既有读取审计，
但日期、种子和候选 ID 自身不是未来市场反馈。仅这些元数据的 detail/export/
artifact 读取从实际日期污染中分开；events、evaluation、source 等读取不会获此
例外，optimizer 仍不能直接读取隐藏详情。

冻结时与首次登记计划时分别检查 exposure，检查与写入由同一 SQLite writer lock
保护。冻结后、登记前新增暴露阻止登记。已登记计划完成后读取留出结果不会篡改
原冻结或原计划；当前 exposure 报告会变为已见，下一实验不能靠改名再次声称未见。

## 验证与边界

新增 21 项测试，与现有登记/评估共 82 项通过：根候选、迟到初始化、旧 revision、
未完成计划、未知清理、提交前/后恢复、两连接竞争、计划/样本原子登记、改名加跑
拒绝、跨实验 exposure、旧调优日期复用、元数据/结果读取区别、变更定义拒绝。

这不证明真实历史数据未被用户在系统外看到；已有外部知识需要通过原 owner exposure
声明如实记录。真实数据、模型、资源计量和完整实际比较指纹仍待验收。比较晋级
CAS 已通过合成资格契约验收，预定[基线复用](lagent-baseline-reuse-contracts.md)消费
路径亦已接入；真实资格生产及正式留出执行尚未接通。
