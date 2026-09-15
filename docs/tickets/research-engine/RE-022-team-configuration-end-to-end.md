---
id: RE-022
status: done
depends_on: [RE-012, RE-014, RE-021]
adrs: [0009, 0021, 0027, 0028, 0053, 0054, 0055, 0056]
---

# 验收 Team 配置端到端链路

## 结果

使用临时 Catalog、Advisor 配置和伪执行器完整证明 WebUI/API 发布的 Team 能进入 Daily Team Set，并由现有 Research Cycle 按共享 Snapshot、唯一 Agent Invocation、独立 Decision Pipeline 和独立报告规则执行。

## 范围

- 建立离线端到端 fixture：读取 Agent 目录、发布第二种风格 Team、每日启用、触发 Daily Batch、读取独立 Team Reports。
- 验证新增 Agent Manifest 或 Agent 新版本无需修改 API/前端注册表即可出现在最新 Agent 目录；既有 Team 仍固定旧版本。
- 验证完全相同发布重试幂等、修订自动递增、旧 Manifest 永久保留、Catalog 重载和进程重启后状态一致。
- 验证启用 Team 新版本原子替换旧版本，不影响其他 Team；取消全部后调度明确跳过且零 Provider/Codex 调用。
- 在两个包含重叠 Agents 的 Team 上验证共享同一 Snapshot，相同 Invocation 只执行一次，Team Pipeline 与报告保持独立。
- 验证任一 Agent 失败只阻断依赖 Team，其他 Team 继续；Cycle Index 只导航，不比较或聚合结论。
- 更新 Research Engine 运维文档，说明创建、修订、每日启用、取消启用和历史查看的中文操作流程。

## 不包含

- 实际修改生产 Team/Advisor 配置、真实联网 Provider、真实 Codex 长任务或真实 08:30 等待。
- Agent 在线创建、Team 删除/归档、跨 Team 比较、自动风格推荐或真实交易。
- 账户、权限、API Key、远程部署或通用 Team 管理产品化。

## 验收条件

- [x] 一条离线测试贯通 Web API 领域服务、Manifest Catalog、Daily Team Set、Daily Batch 和独立 Team 报告。
- [x] 重叠 Agent 只产生一个 Invocation/Finding；两个 Team 各自拥有完整、互不引用的 Decision Stage 和 Report artifacts。
- [x] 发布、修订、重启恢复、每日版本替换和空集合跳过均有稳定自动化覆盖。
- [x] WebUI 前端全套测试和构建通过，所有用户可见 Team 文案为易懂中文。
- [x] 完整 `tests/advisor`、仓库 Node 离线自检和 `git diff --check` 通过。
- [x] 验收过程不修改真实 `config/research/teams/`、`config/advisor.yaml`、报告或 Advisor 数据库。

## 可能触点

- `tests/advisor/research/test_team_configuration_end_to_end.py`
- `tests/advisor/research/test_end_to_end.py`
- `tests/advisor/test_research_web.py`
- `frontend/src/App.test.tsx`
- `docs/research-engine-operations.md`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor
cd frontend && npm test -- --run && npm run build
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
git diff --check
```
