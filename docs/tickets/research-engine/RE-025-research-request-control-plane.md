---
id: RE-025
status: done
depends_on: [RE-002, RE-013, RE-023]
adrs: [0072, 0090, 0091, 0092, 0095, 0097, 0098, 0099]
---

# 建立 Research Request 与 Record 控制面

## 结果

SQLite 成为所有手动、定时、CLI 与历史研究生命周期的唯一控制面，能持久记录请求、进度、终态、取消、再次研究和报告索引，并支持稳定分页查询。

## 范围

- 增量建立 Research Request、Research Record、阶段进度和必要事件表；迁移不得破坏既有 Cycles、Snapshots、Artifacts、Findings 或 Reports。
- 每条 Request 固定一个精确 Team 版本和一个 Scope-compatible Research Subject，并记录 origin、accepted/requested time、Scope-specific Boundary、队列位置所需时间、阶段、Agent 完成计数、Decision Stage、终态、有限 reason code、publication time 与 artifact reference。
- 生命周期至少包含 `queued`、`running`、`passed`、`partial`、`blocked`、`failed`、`cancelled`，并为 running Request 单独记录 `cancel_requested` 意图。
- 每次有意识触发均创建独立 Request；不按 Team、Subject 或日期限次/去重。只允许 submission identity 对同一次传输重试幂等。
- `rerun_of` 只关联新旧 Request；再次研究复制精确 Team 与 Subject，但不复制原 Boundary、Snapshot、状态或报告。
- 提供原子 submit、claim、transition、progress、request-cancel、complete 和 read/query 深接口，调用方不直接拼接控制面 SQL。
- 对既有完整发布 Cycles 做幂等索引，标记 legacy origin 并引用原不可变 artifacts，不移动、重写或伪造请求时间。
- 查询支持服务端游标或页码分页、稳定 Team ID、精确版本和状态过滤；报告默认按 publication time 倒序且没有 recent-100 硬上限。

## 不包含

- 常驻 Research Service、Provider、Codex、Web API、WebUI 或报告 Markdown 渲染。
- 删除历史记录、重新执行 legacy Cycle 或把 report directory 变成第二个可写控制面。

## 验收条件

- [ ] 并发相同 submission identity 只产生一条 Request，不同点击即使 Team/Subject/日期相同也产生两条。
- [ ] 非法状态跳转、终态取消、跨 Scope Subject、未知 Team 与倒退时间均被拒绝且事务不留半状态。
- [ ] queued 可原子取消；running 只记录取消意图；所有终态永久可查询。
- [ ] rerun 产生新 ID、新 accepted time 和空 Boundary，并保留只读 `rerun_of` 链接。
- [ ] legacy Cycle 索引可重复运行且不重复、不改文件；passed/partial 有报告引用，其他终态没有伪报告。
- [ ] 超过 100 条 fixture 可完整分页、无重复无遗漏，并按 publication time 稳定倒序。

## 可能触点

- `advisor/db/schema.sql`
- `advisor/research/repository.py`
- `advisor/research/artifacts.py`
- `advisor/research/reporting/cycle.py`
- `tests/advisor/test_db_schema.py`
- `tests/advisor/research/test_control_plane.py`
- `tests/advisor/research/test_team_reporting.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/test_db_schema.py tests/advisor/research/test_control_plane.py tests/advisor/research/test_team_reporting.py'
```
