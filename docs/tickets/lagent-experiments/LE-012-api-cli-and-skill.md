---
id: LE-012
status: in_progress
phase: phase_1
depends_on: [LE-010, LE-011]
---

# 实验 API、ahunter CLI 与 skill 同步

## 结果

所有实验操作通过现有本地 API 和 ahunter CLI 完成，命令返回可追踪状态而非伪完成。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D12）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-010](LE-010-durable-episode-orchestration.md), [LE-011](LE-011-evaluation-and-promotion.md)

## 范围

- 新增实验 create/show/list/preflight，candidate register/list/show，tests submit/list/show/cancel/rerun，compare/tree/export/calibrate/finalize-holdout 对应 API 与命令。
- 复用本地 origin/JSON 校验、request identity、cursor 和下载机制；有副作用操作不自动重试，幂等重复由服务器处理。
- 实验字段与能力错误完整透传，不在 CLI 重写资金、日期、成交或评分；运行状态与 HTTP 状态区分。
- 更新 advisor/cli/catalog.py、维护工具对新路由模块的发现、skills/ahunter/SKILL.md 及 references/workflows.md，重生成 commands.md。
- 提供输出中的来源/日期信任/成本未知/资料缺失说明，导出 manifest 和产物哈希；没有连接、数据或模型能力不能启动假执行。

## 验收条件

- [x] 每个新增 API operation 都有 CLI 映射，maintenance --check 通过。
- [ ] 请求序列化、空/非法字段、分页、异步取消、幂等冲突和导出文件有端到端契约验证。
- [x] 安装命令与个人 skill 链接正确，不隐式启动 API、服务或 Chrome。
- [ ] CLI 返回的测试 ID 可查询所有 attempt、有效性与结果；命令参考与真实 catalog 一致。

## 可能触点

advisor/research/experiments/api.py（新增）；advisor/web/api.py；advisor/cli/catalog.py；advisor/cli/maintenance.py；skills/ahunter/

## 验证

API/CLI 固定 fixture 测试及维护检查；代码交付按仓库要求 clean-shell self-test 后重启已有 API 并验证受影响入口。

## CLI / skill 影响

本 ticket 负责集中同步，前置 ticket 若提前暴露运行能力也不能等待本 ticket 才同步。

## 2026-09-09 草案与审计证据入口

新增原例 preset、草案 create/list/show、预检/历史、记录概览/审计明细、Test 事件、
manifest/原 bytes 下载及比较反馈共 12 operations。隐藏明细读取使用同源 POST
和明确 audit_identity，审计必须先提交；不以 GET 或元数据详情夹带运行结果。
预检始终区分 HTTP 成功与业务 blocked，不生成 Request/Test/队列或模型调用。
详见 [API/CLI 契约](../../research/lagent-experiment-api-contracts.md)。

catalog 已增至 63 operations，维护工具发现新模块并重生成命令参考。候选/计划
写入、submit/cancel/rerun、calibrate、selection/finalize-holdout、tree 和生产运行
尚未接通；后续验收项保持未勾选，本 ticket 为 in_progress。

新增 27 项 API/CLI 测试，相关 111 项通过；完整 self-test 为 152 Node＋1717
Python 通过。安装的 runtime CLI 已发现 63 命令，个人 skill 链接正确；API 重载
后实际 preset/list 命令 HTTP 200，OpenAPI 可见全部 12 个实验 operations。

## 2026-09-09 候选包上传、登记与查询

新增 candidates register/list/show：JSON 上传声明清单、规范 base64 原 bytes、
提案元数据和可空 diff，复用本地同一封存算法及原登记事务，不接受主机源目录。
包和提案/父链保持各自身份，注册不执行代码、不创建 Test；冻结后仍拒绝新增候选。
show 核对原包字节/清单/父链，list 保留原分页上限并只返回提案元数据。

新增 28 项测试，与原 API/CLI/登记/封存共 165 项通过。catalog 增至 66 operations，
实验命令共 15 个，命令参考与 skill/workflows 同步。计划、运行控制、标定和选择
写入尚待接通，ticket 继续 in_progress。详细输入及原字节语义见
[API/CLI 契约](../../research/lagent-experiment-api-contracts.md)。

## 2026-09-09 历史包、定义解析与完整样本计划

新增 bundles import、definitions resolve、plans register。显式本地包导入保留原文件
与结构化失败 ID；解析按初始研究时间核对封存日历和索引，保存原配置、来源资格、
时点与不可变 resolved/blocked 决定。成功定义和解析记录同事务提交。完整交错计划
复用原登记/冻结规则，plan 与所有 Tests 原子登记，状态 created，不进入共享队列。

新增 33 项测试全部通过；此前导入/解析/计划与原候选/API/CLI/登记及 CLI 维护
相关 176 项通过。补充慢包封存测试证明导入等待期间其他 API 请求仍可响应。
含实际 create_app 首次产物目录接线、CLI 全流程、损坏/过期原文、篡改索引、跨实验、
冻结、缺样本/次序/条件和提交前后故障恢复。catalog 为 69 operations，实验命令
18 个，skill 与 workflows 已同步。完整运行、标定、选择写入与正式准入仍待后续；
LE-012 保持 in_progress。

最终 clean-shell self-test：152 Node＋1778 Python 通过，1 条既有弃用提示。
API 重载为 PID 26760，六个健康/目录接口均 200；live OpenAPI 含完整 18 个实验
operations，新入口空输入均在存储前拒绝。真实库未创建实验记录。

## 2026-09-09 Test 取消与关联重跑

新增 tests cancel/rerun 两个 API/CLI 操作。取消保存原身份、目标和原因；未入队且
无旧 admission/worker 的 Test 同事务写取消事件与 receipt，已入共享队列的工作
同事务写原取消事实/意图与 receipt，由 ResearchService 完成清理。202 明确 pending，
未知物理状态不报终态；等待评估的 Test 返回原服务关闭，不重放 Episode 或生成评分。
终态取消请求只留痕，已有 completed/failed/blocked 结果不改写。

重跑复用原 Registry 的 linked Test：必须原 Test 终态，保留原样本/计划/候选链，
eligible_for_original_comparison=false，新记录 created、不入队。留出与冻结后新增
重跑拒绝，既有重跑重试返回原 ID 及当前状态。取消请求重试亦返回原 receipt 与
当前状态，原身份变更内容/目标冲突。

新增 28 项控制测试；此前与定义/API/CLI/登记/共享服务及 CLI 维护共 162 项回归
通过，最终控制/选择测试共 49 项通过。纯取消回执的审计保持元数据分类；额外字段、
关联或附件不豁免未来暴露，避免操作检查误消耗留出资格且不开放结果旁路。
覆盖实际应用接线、未执行/排队/运行/等待评估/终态、未知物理清理、缺调度索引、
冻结和留出限制、原事件/样本保留、提交前后故障，以及 CLI 202→200 状态观察。
catalog 为 71 operations，实验命令共 20 个，skill/workflows/命令参考已同步。
执行提交、标定、选择写入、完整生产准入及最终验收仍待完成，ticket 保持进行中。

最终 self-test 为 152 Node＋1806 Python 通过；API 重载 PID 37813，六个健康/
目录接口均 200，live OpenAPI 有完整 20 个实验操作。新增入口空输入在存储前拒绝，
实际库没有为验证创建实验或发送真实取消/重跑。

## 2026-09-09 初始基线、比较选择与留出冻结

新增 selection initialize/show/apply/finalize-holdout 四个 API/CLI 操作。初始化
沿用每实验唯一 canonical 身份，在 Test 开始前绑定同实验根候选和封存定义；读取
只返回当前指针 allowlist，不返回样本、比较明细或暴露报告。比较写入首先拒绝留出/
未知反馈范围，再由原 Controller 重核成绩/资格/指纹和选择版本；并发比较只能晋级
同一基线一次。不充分、过期或冻结决定不移动指针，HTTP 成功不代表晋级。

冻结继续要求原版本、全部 Test 终态、物理清理已知、原候选字节和留出未见证明。
公开链路已验证晋级后保留初始/最终候选，随后只登记唯一完整留出样本计划，不运行
Test。三个写入操作在工作线程完成核验与事务，原身份重试保留决定与关联。

新增 27 项接口测试通过；此前选择/API/CLI/定义和维护相关回归 144 项通过。
包括并发 CAS、原字节丢失、伪造资格的自洽哈希、未知清理、未来暴露、迟到比较、
唯一留出计划、提交前后故障、实际 create_app 接线与 CLI 业务状态。正向晋级使用
合成 accepted-assessor 合同验证消费者，不等于正式样本生产者已验收。
catalog 为 75 operations，实验命令 24 个，skill/workflows/参考同步。正式提交、
比较/标定生产、tree、完整模型/数据/资源准入与首版验收仍待完成。

最终 self-test：152 Node＋1833 Python 通过。API 重载 PID 43918，六个健康/
目录接口均 200；live OpenAPI 含 24 个实验操作，新增写入空输入在存储前拒绝，
API/安装 CLI 的 selection show 对未知实验为 404。真实库未登记实验或选择事实。

## 2026-09-09 比较登记与不可变结果入口

新增 comparisons freeze/complete 两个 API/CLI 操作。登记先绑定原计划、候选对、
当前选择版本和逐 Task 条件；所有附件哈希必须携带精确原 bytes，即使相同哈希已
存在，也不以仅知哈希获得另一记录的内容关联。完整上传先验证再封存，声明不授予
正式运行资格。新比较必须在原 Tests 执行前固定；修复不充分结果须新计划并关联
旧结果，留出及未知范围不能通过修复链接绕过反馈限制。

公开 complete 等待全部原 Test 终态后生成 canonical 结果，锁定各 Test 首个评估
身份；缺评估或不合格样本保留 inconclusive/null，后续重评不替换。已有内部不充分
快照的重试仍返回原结果。响应只含结果身份、状态和允许的聚合反馈，样本明细仍走
审计读取；生成结果不自动移动基线，也不启动执行。

新增 24 项比较接口测试；与评估/晋级/定义/API/CLI 相关回归 158 项通过，最终
修复链接的范围限制纳入完整自检。实际 create_app 隔离流程覆盖登记比较、取消
原样本并生成不充分结果。包括原字节哈希、早执行拒绝、首次评估固定、合格消费者
合同、选择版本、跨实验/留出限制、提交前后故障与无 --json 的 complete CLI。
合格正向案例仍为合成 accepted-assessor 消费者验证，真实生产者尚未验收。

catalog 已增至 77 operations，实验命令 26 个；命令参考、skill、workflows 和 API
契约同步。执行提交、标定入口、tree、实际模型/数据/资源准入与首版验收仍待完成，
LE-012 保持 in_progress。

最终 clean-shell self-test 于 2026-09-09T13:41:53.117Z 完成，报告 ok=true、
exitCode=0（本次 startedAt 为 13:37:39.435Z）。maintenance 覆盖全部 77 operations；
15 个本轮文件的空白与本地文档链接、安装 CLI 的 77/26 命令和个人 skill 链接通过。
API 重载后 launchd PID 50295、状态 running，六个健康/目录接口均 200。live OpenAPI
含全部 26 个实验操作；比较空登记及结果夹带输入为 400，未知范围 complete 在 API
及安装 CLI 均为 404，CLI 无需 --json。真实库仍无实验草案；正向写入仅在隔离测试中
验证，未启动真实模型、Episode 或正式准入。

## 2026-09-09 标定条件冻结与结果派生入口

新增 calibrations freeze/complete 两个 API/CLI 操作：公开冻结必须绑定原 initialize
选择及相同初始根候选/定义，保存价格表和全 Task 无模型资源证据；所有引用须上传
精确原 bytes。比较和标定共用上传校验，只有哈希不能取得新证据关联。新 campaign
在写事务内检查选择开放和当前版本；所有定义 Test 尚未执行/建立预算。原候选 bytes
重新校验；canonical campaign/result 的同输入重试不随执行、后续核对或最终冻结改写。

complete 只消费原重复、完整阶段和已结算成本，保留最大研究成本与原比例/预留/取整
公式。缺阶段、缺测量、未知 usage、资源失败继续返回结构化 blocked 原因，不丢弃
昂贵或失败样本。两个写入仅返回记录身份/类型与 formal_ready=false；资源/测量/
额度明细仍经审计读取，避免完整定义的隐藏任务尺度从写入响应泄漏。所有操作在
工作线程完成，不执行模型、生成测量或入队。

新增 30 项标定入口测试；标定/比较/API/CLI 和维护相关 122 项回归通过。覆盖原始
bytes、全任务与证据一致性、初始基线、迟到冻结、候选丢失、冻结后幂等、缺测量/
未知用量/超包络、最大值派生及审计、提交前后故障、跨实验/非法上传、实际 create_app
存储接线、CLI 空 JSON complete 和并行请求响应。首轮两处新断言误写既有 finalize
接口状态码，已按其原 HTTP 200 契约修正并复验，没有更改该接口行为。

catalog 为 79 operations、实验命令 28 个；skill、workflows、命令参考和契约同步。
LE-009/012 继续 in_progress：标定入口已经接通，真实费用/资源生产者、执行提交、
tree、UI 和首版真实验收仍待推进。fixture 价格与测量不构成正式运行资格。

最终 clean-shell self-test：152 Node＋1887 Python 通过，1 条既有 Starlette 弃用提示；
本次 2026-09-09T13:53:01.261Z 开始、13:57:26.074Z 完成，报告 ok=true/exitCode=0。
maintenance 覆盖 79 operations；17 个本轮文件空白/本地链接、安装 CLI 的 79/28
命令及个人 skill 链接通过。API 重载为 launchd PID 55555、running，六个健康/
目录接口均 200；live OpenAPI 有 28 个实验操作。标定空登记及结果夹带输入为 400，
未知范围 complete 在 API/安装 CLI 均为 404，CLI 无需 --json。真实库仍无实验草案；
没有为 smoke 创建标定、测量或真实执行。

## 2026-09-09 进化树与节点历史查询

新增 tree、candidates tests/comparisons、selection history 四个 API/CLI 读取，
共 83 operations/32 个实验命令。树保留独立提案与原父链，比较对手另列；初始/当前
基线从原选择历史派生，保留决定不移动指针。所有失败/取消/原重复和关联重跑可分页
查询；基线复用按实际 Test 去重，不把比较引用或 worker attempt 计作新样本。

分页固定记录和事件两个上限，在只读事务内取一致快照。后续状态/新增节点/结果/
晋级不倒填旧页。状态只读经哈希验证的原事件，原包 bytes 丢失不遮掉永久节点；
下载/候选原包查询仍检查完整性。节点及选择历史仅元数据，比较只给原聚合 allowlist，
留出和未知范围不借列表泄漏。收益/成本/阶段/暴露报告继续经原审计入口下钻。

新增 24 项进化查询测试；与 CLI、基线复用、选择及维护相关 109 项通过。覆盖空树、
失败父链、同包独立提案、关联重跑、双上限分页、历史基线与保留决定、父版本/对手
分离、迟到结果、隐藏明细、复用去重、原文缺失、损坏记录、cursor 绑定及实际应用/
安装 CLI 接线。故障测试先被不可变数据库触发器阻止，随后只在隔离 fixture 中移除
对应更新触发器模拟存储损坏；生产约束未改动。最终身份/对手/结果关联核验纳入完整自检。

CLI/skill、生成参考、API 契约与索引已同步。LE-013 从 todo 调整为 in_progress，
此增量是树及运行界面的查询基础，前端页面、键盘/展开交互、Episode 详情与视觉
验收仍待实现；不勾选完整界面验收项。执行提交与真实数据/模型/资源验收也仍待推进。

最终 clean-shell self-test：152 Node＋1911 Python 通过，1 条既有 Starlette 弃用提示。
本次 2026-09-09T14:09:05.432Z 开始、14:13:34.461Z 完成，报告 ok=true/exitCode=0。
maintenance 覆盖 83 operations；13 个本轮文件空白/本地链接、安装 CLI 的 83/32
命令及个人 skill 链接通过。API 重载为 launchd PID 60878、running，六个健康/
目录接口均 200；live OpenAPI 有 32 个实验操作。四个新查询对未知实验返回 404，
limit=201 返回 422，安装 CLI 同样透传 404。真实库仍无实验草案，没有为验证
创建候选、Test、选择或标定记录；本轮未修改前端，不声称页面或视觉验收已完成。
