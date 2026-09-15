# LAgent 固定时间访问契约

2026-09-09：LE-004 内部离线实现中。工具访问边界已有测试，尚未完成 Episode 运行时
集成与真实连接验收。没有模型调用、Tavily 请求、模拟交易或收益。

## 宿主接口

`data/temporal.py` 定义 `Boundary(event_cutoff, snapshot_at, market_timezone)`：
新闻/修订的可用时间不能超过事件界限；历史行情事件须在界限内，其声明公布延迟可在
固定快照时刻前到达。回执按已提交时刻加配置延迟可见。日期型公开材料次日当地零点
可见；无可信历史证明使用采集时间；MX 使用 received_at 并检查 source_created_at；
衍生产品取全部依赖的时间上界，未来修订/因子不能因重新计算而提前出现。

`data/access.py` 提供 `PhaseScope`、`PhaseSession`、`HistoricalQueries`。
宿主固定 experiment/test/phase/generation、产品授权与两个截点，候选参数只有产品、
证券、日期范围和数量。子会话继承同一权限与界限；读取、落审计事务及返回前复核阶段
权限。查询目录只列授予的产品/版本，不列完整数据库存。不可见行的名称、数量、行情、
宿主路径和 provider 异常正文不进入结果或查询审计。必需产品失败阻断计分，可选失败
返回 unavailable；时间门槛失败不能据此缩小正式预检的全任务覆盖。

`Product` 注册是宿主信任边界，数据来源及依赖证明由宿主提供。候选代码不能获得这些
Python 对象、原始 bundle、生产 DB 或通用 record store。当前尚未实现 LE-008 的 OS
隔离，不能仅凭 Python 封装宣称隔离已经完成。

## 日期搜索与连接

`HistoricalSearch.query` 只接收查询文本；服务连接、日期条件、topic/depth/条数、
超时及重试来自固定配置。`TavilyHTTPConnection` 通过宿主连接管理器的注入 resolver
取得临时授权，仅 POST 固定 Tavily endpoint，不跟随重定向。授权值不进入实验配置、
结果、审计或错误。调用前必须经过宿主授权钩子，后续接入 LE-009 预算与运行前置检查。
没有连接返回 search_provider_unavailable，不注册账号、购买数据或切换服务。

[Tavily Search API 官方文档](https://docs.tavily.com/documentation/api-reference/endpoint/search)
描述 end_date 按发布日期或更新日期过滤，并提供 auto_parameters/include_answer；
本适配器固定关闭后两者。文档的 before 描述本身不证明本项目所需的时区和日期包含性。
`DateMapping` 要求显式版本、时区与包含性：已固定同一时区的 fixture 分别验证
inclusive/exclusive；不确定或不匹配时提前收紧 provider 日期两天，并记录
conservatively_tightened。last_included_date 始终是宿主允许的最晚市场自然日，
实际 provider 条件可以更严格；不能把收紧当作完整日期覆盖证明。

08-02 23:05 的最大允许日为 08-01。当日精确时间材料只通过合格数据产品。
显式未来发布日期/更新日期拒绝；无日期材料按已接受的服务过滤信任政策保留，状态
为 date_filter_trusted。并不逐页验证历史存档，也不将其升级成 timestamp_verified。
正文以 untrusted_evidence 返回，不能授权新工具、联网或改变平台指令。

## 缓存、审计与恢复

缓存键包含实验、查询、provider/连接引用/版本、完整设置、两个时间界限、日期映射、
信任政策、保留政策与采集世代。同条件跨候选复用相同响应，但不暴露其他候选的查询
列表、actor/test ID。只有宿主可以读取全部 cache records；候选没有缓存枚举入口。

开始请求前先写不可变请求记录，已有响应不刷新。请求进行中、并发重复或外部结果
未知时返回 search_outcome_unresolved，不隐式重发。授权后瞬时失败仅按配置有界重试。
精确重放只用既有响应；缺响应或正文过期明确不可重放。新世代和新查询不保证搜索索引
与较早现实时间完全一致，结果保留这一限制。

搜索响应正文按 SourceRetention 管理。永久查询审计保存请求、结果状态和哈希，不复制
正文到永久产物；过期可以删除原文字节并保留元数据。一般自有数据查询响应按内部永久
产物保留。所有查询审计链接本 Test；owner/optimizer 隐藏细节政策继续在宿主视图执行。

## 验证与未完成项

`test_experiment_time_access.py` 26 项固定响应测试：双截点、日期可见性、晚修订、依赖
上界、参考生效区间、主子一致、迟到 fence、隐藏数据/错误/目录、搜索未来日期与无日期、
两种日期包含性/未知时区、配置/世代/截点隔离、正文过期、无连接、缓存缺失、受控重试、
禁用搜索、固定 HTTP endpoint/重定向和秘密错误净化。与 bundle 测试组合 52 passed。

LE-005 已提供持久 PhaseClock 并与 PhaseSession 接通（见 lagent-phase-clock-contracts.md）；
完整 Episode 编排、LE-008 的 RPC/进程/网络/文件系统封堵、LE-009 真实预算钩子
和 LE-015 Tavily 实际日期过滤验收尚未完成。没有发现或使用可用真实 Tavily 连接。
工具过滤不能消除模型预训练知识污染；这仍是最终报告必须披露的独立限制。

CLI/skill：未增加 API/命令，catalog 不变。ahunter skill/workflows 已同步开发状态、连接
缺失和信任时间语义。现有业务 LAgent 搜索策略与 Market Daily 新浪唯一实时源均不变。
