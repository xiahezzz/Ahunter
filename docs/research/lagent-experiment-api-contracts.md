# 历史实验 API/CLI

本增量将既有 ExperimentStore/ExperimentQueries 接入原本地 API；不另建数据库、
服务或执行器。命令前缀为 `ahunter research experiments`，API 前缀为
`/api/research/experiments`。完整 wire 字段由
[命令参考](../../skills/ahunter/references/commands.md)生成。

| 命令 | 结果 |
| --- | --- |
| presets original-case | 可编辑原例 config；不解析日历或授予执行资格 |
| create / list / show | 不可变草案、原始输入、校验错误与草案身份 |
| preflight / preflights list | 固定身份的配置/本机库存预检及历史分页 |
| records list | 按 kind 的记录概览与 Test 当前状态，不返回隐藏 value |
| records show | 经审计读取原记录，不拼入未经审计的运行投影 |
| tests events | 经审计分页读取原事件，包括已有 attempt、阶段、账户和费用事实 |
| records export | JSON manifest：原记录、关联、产物哈希/大小/保留状态 |
| records artifact | 经审计读取该记录明确链接的原 bytes |
| comparisons feedback | 现有允许的数值/决定摘要；最终留出和未知范围拒绝 |
| candidates register / list / show | 原字节封存、独立提案登记、父链与包完整性查询；不创建 Test |
| bundles import | 显式本地目录的原包封存/索引，保留来源资格与失败记录 |
| definitions resolve | 按初始研究时点核对原日历，原配置与解析决定不可变保存 |
| plans register | 原定义下完整交错样本集同事务登记，Test 保持 created，不入队 |
| tests cancel | 持久取消请求；已入队工作由原服务完成物理清理，不提前报终态 |
| tests rerun | 从终态原 Test 创建 linked Test，不替换原样本、不入队 |
| selection initialize / show | 固定初始根候选，读取当前基线指针元数据 |
| selection apply | 重核原比较资格并以原选择版本提交决定；不满足条件不晋级 |
| selection finalize-holdout | 冻结原基线选择，允许后续登记唯一留出计划；不执行 Test |
| comparisons freeze | 执行前固定原配对、选择版本和声明运行条件，封存上传的原附件 |
| comparisons complete | 原 Test 全终态后固定首个评估结果；只返回身份及允许的聚合反馈 |
| calibrations freeze | 执行前固定初始基线、完整标定计划及所有任务价格/资源条件 |
| calibrations complete | 从原重复完整阶段和已结算费用派生不可变额度；只返回结果身份 |
| tree | 分页提案父链、状态/角色计数与初始/当前基线标记 |
| candidates tests | 节点全部原 Test 与关联重跑的元数据，复用引用不重复计数 |
| candidates comparisons | 原比较对手、计划和结果身份及允许的聚合反馈 |
| selection history | 原初始化/晋级/保留/最终冻结记录，固定原因代码及比较关联 |

## 草案与预检

创建要求完整外层 `{config, submission_identity}`；config 必须是对象，但配置缺项
或未知字段可作为不可执行草案原样保留并列出 validation_errors。同标识同输入返回
原草案，变更输入返回 409。没有更新旧草案的隐式覆盖路径。

预检以新的 submission_identity 保存新观察；重复标识返回原报告，不重新读当前
库存/设置，也不以新观察覆盖旧结论。配置无效时直接保存配置阻断，不依赖目录加载。
完整配置预检读取当前目录/日线库存/业务设置观察值，仍明确 blocked、model_calls=0、
orders_created=0、return=null。该检查不等于真实 bundle、模型/资源或候选执行验收，
内部 fixture 执行器存在也不能使此草案入口通过正式运行门槛。

list/preflights 使用有界 limit/offset；records 使用原查询层的带过滤绑定及序号上限
的 cursor。分页不能换 experiment/kind，原 cursor 不混入之后新增的记录。

## 隐藏明细及原产物

records show、tests events、export、artifact 均为 POST，要求本地同源 JSON 和
明确非空 audit_identity；不接受客户端 viewer/权限选择。宿主固定选择 owner，
原查询层在隐藏范围读取前提交 exposure，审计失败就不返回内容。稳定身份仅重用
于同一实验、目标、动作及原页参数；另一个动作/页必须使用新身份。GET 不提供
对应明细旁路。跨实验 ID 和错误记录类型先拒绝，不以错误目标写 exposure。

概览不读隐藏值、不写 exposure；登记元数据与实际结果暴露的区别仍由原选择/留出
规则解释。事件读取审计 action 绑定 after/limit；Test 元数据 detail 不夹带运行
账户或成本结果，避免借元数据豁免释放实际反馈。比较反馈只走已有白名单 reducer，
不返回 trace、任务明细或自由文本，不执行晋级。

下载只接受完整哈希及原 record 的 artifact link，不接受主机文件路径。字节先由
ArtifactStore/SourceRetention 校验，Response 不依赖已关闭存储句柄。manifest
保留精确关联/哈希和 exact_bytes、metadata_and_hash_only、integrity_failure
状态；原文过期返回 410，不能靠共享文件仍存在绕过原来源的保留政策。

API 错误包括：400 输入/来源/JSON/大小无效，403 禁止的反馈范围，404 目标不属
此实验或未链接，409 原身份/完整性冲突，410 原 bytes 不可用，422 分页越界，
503 存储或审计暂不可用。写入和审计读取请求体上限为 1,000,000 bytes。
CLI 只序列化并透传 API 结果；不自动重试或启动服务。下载要求新文件路径，返回
真实字节数和 SHA-256，拒绝覆盖已有文件。HTTP 200/201 与业务完成状态分别显示。

## 验证与剩余范围

新增 27 项 API/CLI 测试，经真实 create_app、TestClient、CLI HTTP 序列化与原下载器验证草案、
预检、记录分页、来源绑定、审计失败、冲突、原 bytes/过期/损坏与隐藏反馈范围。
创建及预检不生成业务 Request、共享 queue 工作、实验 Test 或真实账本分录。
与原 CLI/登记/选择测试合计 111 项通过。

首个入口增量新增 12 operations；候选增加 3 个；导入/解析/计划增加 3 个；取消/重跑增加 2 个；选择增加 4 个；比较增加 2 个；标定增加 2 个；进化树查询增加 4 个，实验共 32 个，catalog
合计 83。路由模块已纳入 maintenance 发现。Test submit 及生产
运行接线仍待 LE-012 后续增量；此文不声明完整 ticket 或首版平台已完成。

## 候选包与独立提案登记

`POST /{experiment_id}/candidates` 接受完整 JSON：manifest、files_base64、proposal、
diff_base64、submission_identity。manifest 是原 CandidateInput，包含四类文件路径、
入口、输入/输出契约及允许的研究配置；files_base64 的 key 必须与全部声明文件完全
一致，value 是原 bytes 的规范 base64。零字节文件也保留其哈希。服务端不读取主机
源文件路径、不落地执行代码或安装依赖，首次有效登记可建立产物存储目录。

proposal 精确包含 proposal_id、parent_proposal_id（可为 null）、hypothesis、source；
source 仅记录 manual/coding_task/optimizer 来源，不启动相应工作。宿主计算
package_hash 与 diff_artifact_hash；diff_base64 为原 diff bytes 或显式 null。
路径/文件种类/入口/允许配置、提案结构及全部 base64 校验后才封存内容，不允许
绝对路径、非规范路径、空路径段、NUL、反斜杠或同一路径兼作文件和目录。

上传与本地 seal_candidate 共享同一规范清单/哈希算法，换行、Unicode、非 UTF-8
字节均不改写。同内容包复用原 package 记录，不合并不同提案；父亲使用同实验的
本地 proposal_id，并继续经过原封存/父链/最终冻结检查。package/proposal/links
同一数据库事务提交，失败不留下半套历史；此前已经封存的内容寻址文件可能暂时
无引用，不能据其存在推断登记成功。原身份重试不覆盖内容，提交后响应丢失可核对
同一候选；冻结后仍可确认旧提交，但不能新建优化候选。

`GET /{experiment_id}/candidates` 以固定过滤和原序号上限分页返回提案元数据，不
读取代码 bytes 或隐藏 Test 结果。`GET /{experiment_id}/candidates/{proposal_id}`
使用本地名称（不是 le-candidate-* record_id），核对原包 bytes/清单/关联和父亲后
返回原 proposal/package/links。内容损坏返回 409；只读查询不写暴露记录。原文件和
diff 下载继续使用返回的 record_id 及既有经审计 artifact 命令。

## 历史包、解析定义和完整计划

`POST /{experiment_id}/bundles/import` 接受 `{root, submission_identity}`。root 必须
是显式绝对本地目录；仅读取 manifest.json 和清单声明的来源/数据文件，保留原文件
哈希、时间语义与 fixture_only/pending_independent_review 来源状态。导入成功返回
201 和 bundle；不代表完整覆盖或正式来源验收。清单/文件/行无效时返回 400，detail
含 blocked、issues、failure_record_id、execution_available:false；有可读原清单才
能保存失败原文。缺失目录/不可读清单不能伪造证据。行写入失败整包回滚。文件封存
和数据库处理在工作线程进行，慢导入不占用 API 事件循环；请求仍等待导入结果，
不是执行队列任务。超时后先核对 records list/show，不自动换身份重试。

`POST /{experiment_id}/definitions/resolve` 接受完整 `{config, calendar_bundle_id,
submission_identity}`。不接受客户端已解析日历、as_of 或 complete 声明。宿主根据
各 Task 初始研究时间检查同实验日历包；相同 period 共用最早截止点。原日历文件的
哈希、行数、原行与索引均核对，所有声明日历覆盖及发布时间满足要求才解析，不缩短
请求区间。缺配置、缺覆盖、晚发布、错版本、原字节丢失/损坏或索引被改均留 blocked
解析记录。源存储本身不可访问仍返回存储错误，不生成假的成功定义。

HTTP 200 返回 resolution、definition（阻断时 null）、status、errors 和
execution_available:false。成功 definition 与 resolution 同事务提交；记录保留
原 config、完整解析规格、包/清单哈希、来源资格与各 period 截止时点，旧草案不变。
同身份重试直接读原决定，不重扫新环境；修改原输入返回 409。冻结后禁止新解析
登记，旧已提交结果仍可确认。resolved 不代表日历来源已正式验收或可执行。

`POST /{experiment_id}/plans` 接受 `{definition_id, plan, purpose, selection_id,
submission_identity}`，selection_id 对普通计划显式 null，final_holdout 必须已有
最终冻结选择。plan 含完整规格哈希、plan_id、seed_support 与全部样本身份。原
Registry 核对候选包、角色、完整 candidate×task×repeat 矩阵、交错次序、种子和
冻结条件。计划与所有 Tests 原子提交；201 返回 plan/tests 原记录及 execution_available:false。
Test 状态为 created，没有共享 queue、业务 Request、阶段事件或账本分录；执行
仍须后续准入入口。返回 Test ID 可用既有 records/events 命令检查。登记前/后故障
分别保留无记录/完整记录，原身份重试不新增样本。

## 取消与关联重跑

`POST /{experiment_id}/tests/{test_id}/cancel` 要求 `{submission_identity, reason}`，
reason 为非空字符串，最长 4096 字符。永久 service_control 保存原请求身份、目标
和原因。同身份变更目标/原因返回 409。未入队、没有 worker/admission 记录的
created/preflight/queued Test，取消事件、状态投影和请求记录同事务提交。登记与
取消竞争时在 writer lock 内重查队列/lease/admission；缺失的原调度索引不能证明
未执行，不据此伪造 cancelled。

共享队列工作由原 ExperimentWorkQueue 登记取消意图，请求记录与原 service-cancel
事实/队列标志同事务提交。队列状态也在 writer lock 内读取；终态不重写，重复请求
不重复产生取消事实。waiting/evaluating 返回原服务处理，running 先清理现有候选；
物理状态未知继续 pending，不新建服务、不再运行 Episode 或提前生成评分。

响应含 request、test_id、status、terminal、cancel_pending、queue_state 和
execution_available:false。非终态 HTTP 202，终态 HTTP 200；已有 completed/
failed/blocked 保持原状态。重复同一请求返回原 receipt 和当前状态，不能将 202
当成完成，也不能将收到取消后的 completed 误写为 cancelled。返回内容不包含
隐藏结果、成本或 attempt 投影，详细事件仍走既有审计读取。

纯取消 receipt 的 detail/export 仍登记审计，但属于操作元数据，不消耗未见留出
资格。豁免要求精确 cancel/cancel_request 字段、唯一 typed Test 关联且没有产物
附件；新增字段、其他关联或附带产物的记录不享受豁免，不能用取消记录夹带未来结果。

`POST /{experiment_id}/tests/{test_id}/rerun` 使用同一外层字段；原 Registry 把
submission_identity 作为 rerun_identity，要求原 Test 终态且非 final_holdout。
返回 201，包含原样保留的 task/candidate/sample/plan 链接、rerun_of/reason 以及
eligible_for_original_comparison:false。新记录保持 created，不入队；旧 Test 和
原比较样本不被替换。冻结后拒绝新 rerun，已登记的同身份重试仍返回原新 ID；响应
status 是该重跑的当前状态，可能已不再是 created。取消不受冻结限制。

## 基线选择与留出冻结

`POST /{experiment_id}/selection/initialize` 只接受 `{definition_id, baseline_id}`。
baseline_id 是原 candidate_proposal record ID，不是本地 proposal_id。原 Controller
要求同实验根候选、封存定义和可读原候选字节，在任何 Test 开始或建成本预算之前
固定初始基线。每实验只有一个 canonical 初始化身份，不接受 submission_identity；
同输入可重复确认，改变候选/定义冲突。201 返回 selection 原记录和 execution_available:false。

`GET /{experiment_id}/selection` 在未初始化时返回 selection:null，否则返回当前
选择的 ID/哈希/序号/时间、definition_id、初始/当前候选及包、前一版本、operation、
frozen、formal_ready。只读 allowlist 不返回 samples、request、比较明细或暴露报告，
不写审计。完整原选择记录走既有 records show/export 审计读取。formal_ready 是
该原记录的资格字段，不能当成全局运行准入；execution_available 始终 false。

`POST /{experiment_id}/selection/apply` 接受 `{comparison_id, expected_selection_id,
submission_identity}`。comparison_id 必须是已有 completed comparison result。
API 先执行原 feedback 范围检查，拒绝未知/最终留出范围；Controller 再核对原计划、
样本/运行指纹、有效性、成绩、候选字节与选择版本。自洽记录哈希不能替代真实资格，
客户端不能上传分数或 formal_ready/promotion_authorized 声明。只有满足资格且原
基线版本仍匹配才晋级；不充分、过期、未改进或不稳定均保存原决定而不移动指针。
200 返回 decision 原记录，不把 HTTP 成功冒充 promoted；并发比较至多推进一次
同一基线。每个 comparison 有一个永久决定，重试保留原记录，变输入/重绑版本冲突。

`POST /{experiment_id}/selection/finalize-holdout` 接受 `{expected_selection_id,
submission_identity}`。在同一 writer lock 下核对当前版本、全部 Test 终态、已知
物理清理、原初始/当前候选字节、未见/无重叠且有来源的留出日期。成功返回 selection
冻结原记录和 execution_available:false。冻结是永久选择决定；只允许随后通过
plans register 登记同定义的唯一初始/最终候选留出计划，不创建或执行 Test。计划
登记会再检查新暴露；冻结后优化登记被拒绝，旧提交确认和取消保留。

三个选择写入入口在工作线程完成核验及事务，请求等待真实提交结果。正向晋级测试
使用合成 accepted-assessor contract 验证消费者/CAS，不是正式资格生产者验收；
当前真实 fixture Episode assessor 仍不能产出合格晋级样本或开启正式历史执行。

## 比较配对与原结果生成

`POST /{experiment_id}/comparisons` 接受完整 `{plan_id, baseline, candidate,
submission_identity, previous_comparison, expected_selection_id, runtime_conditions,
runtime_artifacts_base64}`。plan_id 是原 TestPlan record ID，baseline/candidate 是
本地 proposal_id。expected_selection_id 必需，previous_comparison 显式 null 或
原不充分结果 ID；修复必须使用新的原计划。仅 tuning/selection_validation 计划
可登记，旧比较链接也需通过原反馈范围检查。样本开始或建预算之前，同一 writer
lock 下固定配对、原选择版本、全部样本身份和运行条件；原输入重试不改旧配对。

runtime_conditions 显式 null 或精确覆盖 Task 的 TaskComparisonConditions。
runtime_artifacts_base64 为其全部 *_hash 到规范 base64 原字节的精确映射，即便
同哈希已有文件也必须提供原字节。整份声明、key 集合、编码和 SHA-256 一致后才
封存；不能仅知道其他记录哈希就添加可下载的永久关联。允许声明 null 配空 map，
但不具备正式晋级指纹。条件是预定声明，实际资格还须由宿主评估器逐样本证明；
不是上传 formal_ready、收益或执行成功声明。请求仍受 1,000,000 bytes 上限。

`POST /{experiment_id}/comparisons/{record_id}/complete` 只接受空 JSON 对象。
首次生成在原事务内要求全部预定 Test 终态，未完成返回 409 且不登记结果；每个
Test 只选择第一个 evaluation 原记录。终态失败、缺评估或运行资格不足按原算法
生成不充分结果，不作零收益。已有结果直接保留原身份和内容，包含此前内部已保存
的不充分快照；后续重评不会替换样本。结果 identity 对原比较固定，无额外提交标识。

返回 comparison 身份/哈希/序号/时间/状态/资格标志、execution_available:false
和原 comparison_feedback 数值/决定 allowlist。样本、逐任务结果、错误明细和
评估 payload 不从写入接口释放；完整内容仍以返回 result ID 经 records show/
export 审计读取。未知/最终留出范围拒绝。Comparison completed 不是 Test completed，
也不自动晋级；后续需显式 selection apply。两个入口在工作线程完成封存/核验和
事务，不启动服务、模型、回放或共享队列工作。

## 标定条件冻结与原始测量派生

`calibrations freeze EXPERIMENT_ID --json FILE` 对应
`POST /api/research/experiments/{experiment_id}/calibrations`，精确请求字段为：

- plan_id：已登记的 calibration TestPlan 记录 ID，包含一初始根候选、一调优任务及完整重复；
- expected_selection_id：同实验原始 initialize 选择记录，定义和初始根候选须与计划一致；
- table：PriceTable，声明版本、USD comparison_equivalent、来源和逐资源 usage 语义/单价；
- envelopes：每个封存 Task 的 ResourceEnvelope，包含无模型完整回放/评估声明和预定资源尺度；
- artifacts_base64：table.source_hash、所有 tariff.usage_semantics_hash 和 envelope.replay_evidence_hash
  对应的精确原 bytes。既有哈希仍须提供 bytes，不接受额外或缺失引用，也不接受主机路径。

上传沿用 1,000,000-byte 同源 JSON 限制；全部 base64/哈希先验证再写 bytes。宿主按原规格
核对价格版本、完整任务集合、安全系数、资源计量及原证据 JSON；证据声明本身不授予真实
provider 或硬预算能力。当前 formal_ready 始终 false。

每个 Definition 只有一个 canonical campaign，不接受 submission_identity 或通过重命名
重算条件。新登记在同一写事务中检查选择仍开放且为原版本、该定义的全部 Test 仍 created
且未初始化费用账本。重核原候选 bytes。请求原选择 ID 和关联永久保存；同输入重试可在
执行或最终冻结后返回原记录，不得改价格、样本、初始基线或资源包络。已有内部无选择绑定
的 campaign 不会被公开入口原地升级或重绑定。

`calibrations complete EXPERIMENT_ID RECORD_ID` 对应
`POST /api/research/experiments/{experiment_id}/calibrations/{record_id}/complete`。
record_id 是 campaign 记录；CLI 自动发送空 JSON，无 --json 或 submission_identity。
不接受调用方提交 measurements、allocations、完成状态或资格。复用原 Calibrations.complete：
只有全部原重复 completed、原阶段完整结束、各阶段研究成本和环境/评估成本已计量且结算、
无未知用量/资源失败，才生成唯一原结果。失败或不足返回 HTTP 409，detail 为
`{status: "blocked", reason: "...", execution_available: false}`，不是缺项的零成本结果。
不选择较便宜重复，也不以 rerun 替换原样本。

派生继续取最大原研究成本，按封存倍率及阶段数比例加原预留，再作一次向上取整。结果
记录所有原测量身份、费用/阶段快照及核对引用、各任务额度和预算指纹。重复 complete
返回原结果，后续核对不静默重写已生成额度。留出只按预定尺度派生，不用留出模型表现。

freeze 返回 HTTP 201，complete 返回 HTTP 200；两者都只返回
`{calibration: {record_id, content_hash, sequence, created_at, cost_record_type, formal_ready},
execution_available: false}`。写入响应无 measurements/allocations/资源证据详情，不产生
暴露记录。明细与原证据继续走审计 records show/export/artifact；完整定义的资源证据包含
隐藏任务范围，不能通过写入接口绕过审计。这两个操作在线程池中完成，均不入队、不调用
模型、不生成实际测量；真实运行、完整成本生产者与正式资格仍待验收。

## 进化树、节点记录与选择历史

新增四个 GET 操作，默认 limit=50，范围 1～200，接受可空 cursor：

| 命令 | API 后缀 |
| --- | --- |
| tree EXPERIMENT_ID | /{experiment_id}/tree |
| candidates tests EXPERIMENT_ID PROPOSAL_ID | /{experiment_id}/candidates/{proposal_id}/tests |
| candidates comparisons EXPERIMENT_ID PROPOSAL_ID | /{experiment_id}/candidates/{proposal_id}/comparisons |
| selection history EXPERIMENT_ID | /{experiment_id}/selection/history |

所有响应包含 items、total、next_cursor、selection、snapshot 和 execution_available=false。
selection 是该快照最新原选择指针的元数据，不是最后一个“保留基线”决定；后者仍在历史
列表出现。snapshot 同时给出 records_through/events_through。每次读取在一个只读
SQLite 事务中取得一致视图；后续 cursor 绑定 experiment/view/proposal 及两个序号上限。
页面可以改变 limit，但不能换节点、实验或视图；开始不带 cursor 的查询才纳入新记录、
状态、比较结果及基线变化。不同节点列表各自开始快照，界面须留意其观察范围可能不同。

tree 按提案登记顺序分页，不按收益、胜负或是否有结果筛除节点。返回原提案身份、
parent_proposal_id/parent_record_id、hypothesis/source/diff_artifact_hash/package_hash、
test_count、test_status_counts、task_role_counts 和 initial/current_baseline 标记。
同内容不同提案保留独立节点；父节点必须先于子节点且声明/关联相符。候选原文件不可用时
树仍能显示永久身份；其可读性由 candidates show 及审计 export/artifact 检查，不用树
可读代替原包完整性通过。

节点 tests 只从原 candidate→Test 关联列实际 Test 记录；同一基线被多个计划复用不会
多计样本，尝试记录也不计为新 Test。显式 rerun 是独立记录，保留 rerun_of 和
eligible_for_original_comparison=false。返回原计划/定义/任务/角色/purpose/repeat、
原状态与 detail_hidden；状态取快照范围内经完整性验证的最后 status 事件，不信任
可变投影，也不返回该事件的其余内容。节点统计沿用相同身份和状态来源。

节点 comparisons 以原冻结配对为一项，明确 baseline/candidate 及各自 record ID、
plan/selection ID、可空原结果 ID、awaiting_result/completed 状态。既有结果只附
comparison_feedback 的数值/决定 allowlist；缺结果为 null，不生成零收益。父版本
不被比较对手覆盖，后续结果不会倒填旧页快照。未知/留出范围在宿主拒绝，不返回样本、
失效明细、重评或隐藏任务轨迹。

selection history 保留 initialize/promote/retain/finalize 原记录身份、前后指针、
比较 ID 及允许的决定/原因代码。未知自由文本原因不进入公开列表；原自由文本、成绩、
暴露报告仍由审计 detail 获取。查看上述四个视图不会创建 exposure；查看原隐藏
Test/结果/资源详情继续走带 audit_identity 的既有 POST 读取。

这些操作不创建数据库对象、Test、费用或队列工作；大树分批取节点/测试/比较/决定，
不一次下载全部运行 trace。节点计数只汇总实际 Test，概览不汇总或重复计算费用。
空草案/空树保持空列表与未初始化选择，不虚构 optimizer 行为或模拟收益。界面及
可交互 Episode 运行下钻仍属于 LE-013 后续实现，当前只完成查询数据基础。
