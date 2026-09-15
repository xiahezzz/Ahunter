---
id: LE-002
status: done
phase: phase_1
depends_on: [LE-001]
---

# 永久记录、产物存储与幂等事务

## 结果

每次预检、测试、尝试、比较和分叉都可追溯，崩溃恢复不重复冻结、成交、释放或计费。

设计依据：[实施设计定稿](../../research/lagent-experiment-final-design.md)（D09, D11）。数值均来自可配置 preset，不在代码中另写隐藏默认。统一完成标准见[索引](README.md)。

## 依赖

[LE-001](LE-001-specification-and-contracts.md)

## 范围

- 审查未验证 repository.py 和 schema.sql 起步表，提供空库及现有库安全迁移；保留用户已有数据。
- 扩展现有 ResearchRepository/ArtifactStore，保存定义、候选包/提案、TestPlan/TestRecord、阶段、动作、attempt、成本、账本、Comparison、选择事件和 exposure。
- 定义不可变事实及可重建状态投影；保存 actual_at、simulated_at、event_sequence 和 fencing generation。
- 实现内容寻址原子落盘、数据库引用事务、outbox、幂等键冲突检测与 cursor 分页；API 重试不会创建第二笔副作用。
- 恢复追加 attempt，rerun/rescore 新建关联记录；永久留存不得被树隐藏、候选淘汰或默认清理任务删除。

## 验收条件

- [x] 对每类资金/成本副作用进行事务前后故障注入，恢复后恰好一次落账；同键不同内容冲突。
- [x] 孤儿产物与未完成写入不被宣称完成，旧库迁移后记录仍可查。
- [x] 成功、失败、blocked、cancelled、校正/重评均保留原记录；终态不被恢复重写。
- [x] 超过一页的节点和测试可完整枚举，不能只保留最近或最高收益记录。

## 可能触点

advisor/db/schema.sql；advisor/db/migrate.py；advisor/research/experiments/repository.py；advisor/research/artifacts.py

## 验证

通过 repository/trace Interface 验证事务故障、唯一键、产物损坏、迁移与分页，不用生产数据库。

## CLI / skill 影响

定义记录查询/导出语义供 LE-012 接入；记录永久留存与 rerun 语义需同步操作文档。


## 持久化基础阶段记录（2026-09-09）

已实现同一 Research 控制库内的不可变 facts/links/events、可重建 projections、
worker lease generation、追加 worker attempt、事务 outbox 与不可变 delivery receipt；
ResearchRepository.experiment_store 复用原连接，不创建第二个队列。
支持同键内容冲突、稳定 cursor、合法生命周期与终态保护、linked rerun/rescore 记录。
ArtifactStore 补上目录 fsync；迁移保护起步草案/预检，不修改已有业务记录。

测试覆盖七类宿主副作用在六个提交点中断，重复恢复后各保留一条事件；同时覆盖
并发 revision 冲突、旧 worker fence、产物损坏/孤儿、目录持久化失败、旧库迁移、
永久记录禁止改删、分页和 outbox receipt。属于存储层 fixture 验收，尚非真实成交/成本验收。

该阶段尚缺的类型化候选/定义/TestPlan 登记、原文保留策略、宿主查询隔离和 exposure，
现已在下述最终验收中补齐。
详见 [实现进度](implementation-progress.md)。

最新验证：66 项存储测试；完整 self-test 152 Node＋1055 Python 通过。
CLI maintenance / git diff --check 通过；API 按仓库要求重启并核对服务与 Research 接口。


## 完成验收（2026-09-09）

类型化定义/候选包/提案与整组 TestPlan/TestRecord 已实现原子、幂等登记；
查询/下载/导出由宿主隔离隐藏角色，owner 审计先追加 exposure 再返回数据；
外部已知暴露按日期区间保存。来源原文过期先登记不可用，再删除无其他有效引用的字节；
保留原始元数据/哈希、共享永久产物与所有失败/取消记录。

25 项新增 registration/query/source tests 通过；完整 self-test：152 Node＋1080 Python
通过；CLI maintenance 51 operations covered；git diff --check 通过。
API LaunchAgent PID 45674 → 50839，services 及四个 Research catalog 接口均 HTTP 200；
runtime 可加载 Registry/Queries/SourceRetention。逐项证据见 [实现进度](implementation-progress.md)。

完成范围是 LE-002 的存储、登记和查询 Interface；实际阶段/成交/成本/比较算法、
最终留出冻结与 API/CLI 接入按 LE-005～015 实施，集成故障验收由 LE-014 承担。
未运行真实历史 Episode，没有交付未经数据与费用门槛验证的收益。
