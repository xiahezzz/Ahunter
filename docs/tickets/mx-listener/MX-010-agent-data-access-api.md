---
id: MX-010
status: complete
depends_on: [MX-004, MX-009]
adrs: [0011, 0015, 0018, 0062, 0065]
---

# 提供 Data Product Catalog 与 Agent Access API

## 结果

本地 Web API 能完整展示 Agent 可访问的 Data Products、只读 Provider provenance、依赖和 MX Feed scope，并通过 MX-009 的领域服务发布 access revision，而不执行研究。

## 范围

- 提供 `GET /api/research/data-catalog`：按稳定 Product ID 返回所有已发布版本、标题、依赖、Provider IDs、是否支持 feed scope 及 scope contract。
- Provider 只返回 repository-owned 稳定 ID 和显示名；不返回 endpoint、密钥、请求头、响应或运行时连接细节。
- 提供 `GET /api/research/agent-access`：按稳定 Agent ID 返回最新版本、只读历史版本、每版精确 Data Access、被哪些 Team 版本固定及当前阻断原因。
- 响应中的 MX RID Feed 只来自当前 RID Authorization Set 与 Agent Manifest 的精确引用；新增 RID 标记为“尚未分配”，已撤销引用标记为“授权已撤销”。
- 提供 `POST /api/research/agents/{agent_ref}/access-revisions`，请求携带完整 Data Access 和调用者观察到的 RID 配置版本；只调用 `AgentAccessPublicationService`。
- 路径中的 Agent ref、请求字段、数量、字符串长度和嵌套深度全部有界；拒绝额外字段、客户端版本号、Provider 选择和通配 scope。
- RID 配置在编辑期间变化时返回 409，不用过期授权发布；Catalog/base ref 变化或并发 publication 分别映射稳定 404/409。
- 成功返回 `created`、新 exact Agent ref、规范 Data Access 和 impact；不自动发布 Team、不修改 Daily Team Set、不启动 Provider/Codex/Cycle。
- 保持现有 `/api/research/agents` Team 选择接口向后兼容；Agent Access 页面使用专用接口，避免把 Team 的浅目录接口膨胀成通用编辑接口。
- 所有 mutation 执行与 MX-004 一致的本地同源/严格 JSON 保护，错误为有限中文且不泄露 Manifest 正文或绝对路径。

## 不包含

- WebUI、Agent 指令编辑、Team 自动升级、Data Product Manifest 编辑、Provider endpoint 配置或研究执行。
- 多用户权限、审批、远程 API、API Key 或通用插件管理。

## 验收条件

- [x] Catalog 自动发现新增 Product/版本并稳定展示依赖与 Provider provenance，不暴露 endpoint/secret。
- [x] Agent Access 接口展示 latest/history/exact grants/Team impact，并区分未分配与已撤销 RID。
- [x] 发布覆盖成功、幂等、未知 base、未知 Product、非法 scope、配置版本 conflict、原子写失败和并发冲突。
- [x] 任意角色 Agent 可选择任意 Product；API 不产生角色白名单或证据覆盖警告。
- [x] 成功与失败请求均为零 Provider、零 Codex、零 Cycle、零报告、零 Team/Daily Set 变更。
- [x] 现有 Team 配置 API 与 frontend contract 回归通过。
- [x] 测试只使用临时 Catalog、RID 配置和 Agent 目录，不修改真实 manifests 或 RID。

## 可能触点

- `advisor/web/api.py`
- `advisor/research/agent_access_publication.py`
- `advisor/research/catalog.py`
- `tests/advisor/test_agent_access_web.py`
- `tests/advisor/test_research_web.py`
- `tests/advisor/test_web_api.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/test_agent_access_web.py tests/advisor/test_research_web.py tests/advisor/test_web_api.py'
```
