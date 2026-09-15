---
id: RE-019
status: done
depends_on: [RE-014, RE-018]
adrs: [0009, 0028, 0056]
---

# 管理每日启用 Team 集合

## 结果

项目通过一个独立于 Team Publication 的 Daily Team Set 服务管理 08:30 批次使用的精确 Team 版本；集合可以为空，同一稳定 Team ID 最多启用一个版本。

## 范围

- 以 `config/advisor.yaml` 的 `research.default_teams` 作为 Daily Team Set 的唯一持久化位置，并允许该列表为空。
- 提供读取、启用和取消启用操作；所有输入必须是当前 Catalog 中已发布的精确 Team 引用。
- 启用较新版本时原子替换集合中同 Team ID 的旧版本，其他 Team 保持不变；重复启用或取消操作幂等。
- 配置写入采用校验后原子替换，不修改其他 Advisor 配置语义，不写 Team Manifest 或 SQLite Team 定义。
- Team Publication 永不自动调用启用操作，新版本发布后旧版本继续保持原有每日状态。
- Daily Team Set 为空时，08:30 调度返回成功的明确 `skipped` 结果并记录中文原因，不创建 Batch、Snapshot、Provider 请求或 Codex Invocation。
- 手动显式传入 `--team` 的 Research Run 不受空 Daily Team Set 影响；没有显式 Team 的手动 Run 则给出明确校验错误，不伪装成已运行。

## 不包含

- Team 发布、Web API、WebUI 或定时器时间修改。
- 自动选择 Team、自动跟随最新版本或根据报告结果切换 Team。
- Team 间比较、优先级、排序、共识或 Champion/Challenger。

## 验收条件

- [x] Daily Team Set 能在空集合、单 Team 和多 Team 状态间原子切换。
- [x] 同一 Team ID 不会同时出现多个版本，启用新版本准确替换旧版本。
- [x] 发布新 Team 或新版本不会改变每日集合；启用、取消启用的重复请求不产生额外变化。
- [x] 配置重载和进程重启后读取到相同精确 Team 引用，未知或已损坏引用 fail closed。
- [x] 空集合的 08:30 调度明确跳过，测试证明 Provider、Artifact Store、Agent Runner 和 Codex 均未被调用。
- [x] 显式手动 Team 选择继续运行，且现有非空 Daily Batch 行为保持不变。

## 可能触点

- `advisor/config.py`
- `advisor/research/daily_teams.py`
- `advisor/research/cli.py`
- `advisor/scheduler/premarket.py`
- `config/advisor.yaml`
- `tests/advisor/research/test_daily_team_set.py`
- `tests/advisor/research/test_daily_batch.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_daily_team_set.py tests/advisor/research/test_daily_batch.py tests/advisor/research/test_cli.py
```
