---
id: RE-023
status: done
depends_on: [RE-001, RE-018]
adrs: [0070, 0077, 0089]
---

# 建立显式 Research Scope 契约

## 结果

Research Engine 能以一个统一模型表达 Market 与 Security 两种 Research Scope；二者只在 Research Subject 及其直接兼容的输入、结论契约上不同，不复制 Agent、Team 或执行生命周期。

## 范围

- 在领域契约中增加稳定的 `market`、`security` Research Scope，并把 Research Subject 改为显式区分的结构：Market Subject 表示沪深 A 股整体且不携带股票代码，Security Subject 必须携带一只合法六位沪深 A 股代码。
- Agent Manifest 与 Team Manifest 都能声明 Scope；同一稳定 Agent ID 或 Team ID 的所有版本必须保持 Scope 不变。
- Team 的所有 Agent 必须与 Team Scope 完全一致；Catalog 加载、latest-version 解析和运行前校验均 fail closed。
- 对早于 Scope 字段发布的既有 Manifest 永久按 Security Scope 解释，不重写历史 YAML，也不从 instructions、产品依赖或成员关系推断 Scope。
- 新发布 Manifest 必须显式写出 Scope；序列化、哈希、调用键和错误信息包含 Scope，不能用空代码、虚拟代码或 `000000` 表示全市场。
- 保持 Market 与 Security Agent 的 Manifest、Data Access、Run Capsule、Query、版本、重试和审计契约共用同一实现。

## 不包含

- Team 配置页面、Market Data Products、Market Agents、Research Request、Decision Pipeline 或 WebUI 研究启动。
- 改写现有七个 Agent、`a_share_core@1` 或 `normal@1` 的历史 Manifest。

## 验收条件

- [ ] 新 Manifest 缺少 Scope 时拒绝发布；既有无 Scope fixture 仍可加载且稳定解释为 Security。
- [ ] Market Subject 不能携带代码，Security Subject 缺少或携带非法代码时拒绝。
- [ ] 同一稳定身份跨版本改变 Scope、Team 混入异 Scope Agent、运行 Subject 与 Team Scope 不符均 fail closed。
- [ ] Catalog、canonical JSON、内容哈希和 Invocation Key 能区分 Market 与 Security Subject，且没有 sentinel code。
- [ ] 自动化测试证明两种 Scope 复用同一 Agent/Team 生命周期，而不是进入两套注册表或执行器。

## 可能触点

- `advisor/research/contracts.py`
- `advisor/research/catalog.py`
- `advisor/research/capsules.py`
- `advisor/research/agents/runner.py`
- `config/research/agents/`
- `config/research/teams/`
- `tests/advisor/research/test_catalog.py`
- `tests/advisor/research/test_capsules.py`
- `tests/advisor/research/test_agent_runner.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/test_catalog.py tests/advisor/research/test_capsules.py tests/advisor/research/test_agent_runner.py'
```
