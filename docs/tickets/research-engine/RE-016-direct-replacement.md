---
id: RE-016
status: done
depends_on: [RE-015]
adrs: [0001, 0006, 0019, 0024, 0026]
---

# 直接替换外部 TradingAgents 集成

## 结果

A Hunter 的调度器、协调器、CLI 和 Web 读取面只使用新的自包含 Research Engine。外部仓库、图运行时和旧角色适配器被一次性删除，不存在可选择的旧路径。

## 范围

- 将 premarket coordinator 和相关入口切换到 Daily Research Batch/Research Cycle。
- 更新配置模型：使用 Manifest Catalog、Artifact Store 和执行策略位置，删除 `trading_agents` 外部仓库配置。
- 删除 `ExternalTradingAgentsRunner`、`astock_adapter.py`、外部路径常量、`sys.path` 注入、动态导入、固定 16-role 验证和上游 graph state 映射。
- 删除或重写 TradingAgents 专用 runtime bootstrap/health checks；若 A Hunter 自有 Provider 仍需隔离环境，只保留与本项目依赖有关的部分。
- 更新 Python 依赖、命令入口、launchd 模板、Web/report archive 查询和运行手册。
- 用新领域测试替换旧 adapter tests，并保留现有 collector、ledger、profile、质量和报告安全测试。
- 对改编的 Apache-2.0 内容保留适用的版权与 NOTICE。
- 更新 `agents.md` 的 Advisor operations，使其只描述新 Research Engine 的真实启动与失败规则。

## 不包含

- 兼容层、feature flag、shadow run 或旧实现 fallback。
- 删除用户已有 SQLite、reports、profiles、ledger 或 collector 数据。
- 改变被动 MX collector 的授权与操作规则。

## 验收条件

- [x] 代码、配置、测试、命令和文档不再包含外部仓库绝对路径或 `tradingagents` Python 导入。
- [x] 运行入口无法选择旧 graph，并且新 Engine 失败时明确 blocked/failed。
- [x] `config/advisor.yaml` 不含 `trading_agents` 块，配置测试同步更新。
- [x] launchd 使用项目自己的运行环境和新入口；渲染与管理测试通过。
- [x] 现有用户数据在新 schema 下保持可读，直接替换不执行破坏性清理。
- [x] 完整 Python 测试和 Node 离线检查通过。

## 可能触点

- `advisor/agents/astock_adapter.py`（删除）
- `advisor/runtime_env.py`
- `advisor/coordinator.py`
- `advisor/config.py`
- `advisor/scheduler/`
- `advisor/web/api.py`
- `config/advisor.yaml`
- `pyproject.toml`
- `agents.md`
- `tests/advisor/test_astock_adapter.py`（替换/删除）

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
rg -n "TradingAgents-astock|import tradingagents|from tradingagents|ExternalTradingAgentsRunner|repository_path" advisor config tests pyproject.toml agents.md
```

最后一条 `rg` 应无命中；若某条版权说明必须保留，应限制在 NOTICE/ADR，而不在运行代码或配置中。
