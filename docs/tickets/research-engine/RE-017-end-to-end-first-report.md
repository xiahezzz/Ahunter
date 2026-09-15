---
id: RE-017
status: done
depends_on: [RE-016]
adrs: [0008, 0010, 0017, 0025, 0027, 0028]
---

# 完成端到端验收并生成首份报告

## 结果

使用 `a_share_core@1`、一个明确的 6 位 A 股代码和固定 `as_of`，从公开数据构建 Snapshot，通过本机 Codex 完成七个 Findings 与公共 Decision Pipeline，并发布第一份可验证的 Team Report。

## 范围

- 建立全链路 fixture 测试：Catalog → Products → Snapshot → Agents → Pipeline → Team Report → Daily Team Brief。
- 增加运行前检查：Catalog、数据库、Artifact Store、公开来源连通性、本机 Codex 会话、执行策略与输出目录。
- 提供并记录正式启动命令；标的代码在执行此 ticket 时明确给出，不由模型选择。
- 先执行单 Subject/单 Team live run，避免首份报告被多 Subject 并发噪声影响。
- 验证所有 Artifact 哈希、evidence 引用、版本指纹、质量结果、Team 目录和完成标记。
- 记录总耗时、每阶段耗时、查询次数、Attempt 数与阻断原因，但不改变结论。
- 更新操作文档，说明手动运行、查看报告、blocked 排查和每日调度命令。

## 不包含

- 在任一质量门失败时伪造或补写研究建议。
- Team 比较、第二种风格 Team 或自动交易。
- 为通过验收而放宽 Schema、时间边界或质量规则。

## 验收条件

- [x] fixture 端到端测试完全离线、稳定重复，并覆盖一个成功 Team 与一个 blocked Team。
- [x] 运行前检查全部通过后才启动 live research；失败时生成状态记录而非结论。
- [x] 首次成功运行恰好产生七个 required Findings、固定 Pipeline 的全部 Stage Artifacts 和一个 `a_share_core@1` Team Conclusion。
- [x] `cycle.json`、`index.md`、`teams/a_share_core@1/conclusion.json` 和 `report.md` 均可校验且互相引用一致。
- [x] `index.md` 与 Daily Team Brief 没有新增模型结论或跨 Team 内容。
- [x] 报告不引用晚于 `as_of` 的数据，所有事实 claim 可回溯至 Snapshot evidence。
- [x] 全量 Python tests、Node 离线检查和 `git diff --check` 通过。

## 可能触点

- `tests/advisor/research/test_end_to_end.py`
- `advisor/research/cli.py`
- `pyproject.toml`
- `docs/research-engine-operations.md`
- `reports/<date>/<cycle_id>/`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
git diff --check
```

Live 命令由 RE-016 确定的 CLI 提供，并在本 ticket 的操作文档中固定；验收记录必须包含实际输出路径和 Cycle ID。

## 验收记录

- Preflight：`status=passed`；Catalog、数据库、Artifact Store、公开来源和本机 Codex 均通过；CLI `codex-cli 0.144.0-alpha.4`。
- Cycle：`cycle-202608060830-600519-live8`；Subject `600519`；`as_of=2026-08-06T08:30:00+08:00`。
- 输出目录：`reports/2026-08-06/cycle-202608060830-600519-live8/`。
- Agent 层：恰好 7 个 required Findings，7 个 invocation 均首轮 `passed`，每个 Finding、query-log 和 Capsule 均有 Artifact hash。
- Decision Pipeline：10 个 stage 全部 `passed`，其中 8 个 Codex stage 各 1 次 passed Attempt；最终只有 1 个 `a_share_core@1` Team Conclusion。
- 发布物：`cycle.json`、`index.md`、`complete.json`、`teams/a_share_core@1/conclusion.json`、`report.md` 及 sidecar hash 均存在并通过 SHA-256 校验。
- 运行控制面记录总耗时约 25 分 40 秒；Agent query-log 查询次数为 0，所有事实均来自 sealed Snapshot products；总技术 Attempt 数为 15（7 Agent + 8 Codex Decision Stage）。
