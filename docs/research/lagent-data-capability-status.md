# LAgent 历史数据能力状态

2026-09-09：正式运行 blocked。内部导入与 fixture 覆盖检查已实现，未验收真实历史包，
未产生模型调用、Episode 或收益。LE-003 保持 in_progress。

## 实际输入与时间语义

[机器可读库存](../../reports/lagent-tests/data-readiness/20260909T021801Z/local-capabilities.json)
仅按 manifest 文件名检查 `data/`、`config/research/`、`reports/lagent-tests/`，
未找到 manifest JSON/YAML。没有据此宣称整个本机不存在数据；普通日线和官方规则
参考文件不构成已验收历史包。本轮没有提取 MX 私有事件正文或行情 SQLite。

| 现有能力 | 源码证据 | 历史实验限制 |
| --- | --- | --- |
| 新浪原始日线、因子 | `advisor/market_daily/providers/sina.py:159,411` | source_at=fetched_at，不能倒填成交易当日公开时间 |
| 新浪证券清单 | `advisor/market_daily/providers/sina.py:516` | 当前 active 清单不能证明历史全市场、退市/ST 状态 |
| 本地日线查询 | `advisor/research/providers/local.py:149,150` | 同时限制 source_at/fetched_at，不能凭 trade_date 提前释放 |
| MX 证据查询 | `advisor/evidence/mx_adapter.py:413,414` | received_at 及可选 source_created_at 受 cutoff 限制；不证明更早可见版本 |

新导入器分别保留事件、初次公开、版本公开、采集时刻与来源文件哈希。缺少历史
时间证明时按采集时刻可用；历史声明仍需独立审查。覆盖结果和正式 readiness 分开，
目前正式 readiness 始终为 false，不存在通过改 origin 绕过的运行入口。

## 官方规则参考：已取得与待核验

上交所与深交所的 2026 交易规则通知均规定 2026-07-06 生效；当期规则包不能
直接沿用旧规则。来源：[上交所通知](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)、
[深交所通知](https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html)。

已下载[深交所规则原始 PDF](../../reports/lagent-tests/data-readiness/20260909T021801Z/szse-2026-rules.pdf)，
282084 bytes，SHA256 `9b66f8b0db70f84a25ef1ccb4ee2351001724e408117552d75f6d8993483c586`；
状态 downloaded_unreviewed，尚未转换为覆盖各板块/状态的完整规则包。
[下载尝试记录](../../reports/lagent-tests/data-readiness/20260909T021801Z/source-downloads.json)
中的上交所附件尝试使用了未成功确认的路径，HTTPError 不能证明官方附件不存在。
中登旧上海费用 PDF 此次下载也未成功，不能当作完整费用验收证据。

税务机关的[2023 年第 39 号公告](https://shanghai.chinatax.gov.cn/tax/zcfw/zcfgk/yhs/202308/t468451.html)
说明自 2023-08-28 减半征收证券交易印花税；完整费率包仍须核对基准税率、征收方向、
过户费与佣金包含关系、舍入/最低费用及生效区间，不根据搜索摘要补齐。

## 内部交付与剩余工作

- `advisor/research/experiments/data/` 提供 manifest/行契约、封存与索引、覆盖报告；
  报告按请求任务和完整证券清单保留缺口，不缩短日期或删掉缺数据证券。
- fixture 检查覆盖重复键/自然键、哈希、时区、晚修订、原始价格、日历、分钟、
  竞价与逐笔重建、状态和企业行动缺口。09:25 竞价结果核对事件时间与 09:25:05 可用截止。
- 每条索引保留来源文件哈希，读取复核其时间语义。覆盖导出进入不可变事实和 ArtifactStore，
  具备 fixture 通过的报告仍保存正式 blocked 状态。
- 缺真实历史全市场/日历、分钟、开收盘竞价及完整队列包；需取得并核验时间版本与完整覆盖。
- 完整官方规则/费用、独立来源审查放行、临时停牌与复牌区间、逐笔与分钟/日线的全面
  交叉核对尚待完成。fixture 中缩短的交易时段与参数只服务代码检查。
- LE-004～015 可继续离线实现；正式 Episode 仍须通过真实数据、成本与端到端门槛。

CLI/skill 影响：没有新增 API，因此 51 项 catalog 不变。ahunter skill/workflows 已同步
内部导入/覆盖的可用范围及未放行门槛。独立导入不改变 Market Daily 的新浪唯一实时源。
