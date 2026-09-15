---
id: MX-009
status: complete
depends_on: [MX-008]
adrs: [0015, 0023, 0065, 0067]
---

# 实现不可变 Agent Data Access Publication

## 结果

用户可基于任一精确 Agent Manifest 版本只修订 Data Product 与 Feed scope，并原子发布下一个不可变 Agent 版本；指令、输出契约、预算和实现保持逐字节等价。

## 范围

- 在领域契约中引入 `AgentDataAccess`/`ProductAccess`：每项固定一个精确 Data Product 版本及可选、产品特定的 feed scope。
- 兼容读取现有 `required_products` Agent Manifests，并在内存中规范化为无 scope 的 Data Access；不覆盖、重写或删除既有 Manifest 文件。
- 新发布版本只使用一个规范表示，拒绝同时提供 legacy 与新字段、重复产品、未知版本、空访问集合和不受产品支持的 scope。
- `mx_events@2` 必须固定至少一个唯一、当前已授权的 RID Feed；禁止 wildcard、范围表达式、“全部当前/未来 RID”和已撤销 RID。
- 任何 Agent 可选择任何已发布 Data Product，不使用角色白名单；产品依赖只用于 Engine 物化，不自动进入 Agent 可查询集合。
- 建立深 `AgentAccessPublicationService`：输入 exact base Agent ref 与新 Data Access，复制 title、instructions、details schema、query budgets、implementation/code path 不变，系统分配下一个版本。
- 使用 agents 目录内专用锁和同目录原子创建；相同发布幂等返回既有版本，并发不同修订按明确 conflict 失败，不覆盖文件。
- 旧 Agent 版本永久保留；现有 Team Manifest 与 Daily Team Set 不自动升级、不改写。
- 发布只创建 repository-owned Agent Manifest，不创建 SQLite、Snapshot、Capsule、Invocation、报告或 Codex 任务。
- 提供只读 impact 结果：哪些 Team 版本仍固定旧 Agent、哪些 RID scope 当前已撤销；不自动采取修复动作。

## 不包含

- 新建 Agent、编辑 instructions/title/output schema/budget/code、删除/归档 Agent、自动修订 Team 或启动 Research Run。
- Provider 选择权限；Provider 只作为 Data Product provenance 展示。

## 验收条件

- [x] legacy Agent Manifests 继续按原字节工作；发布 access revision 只新增一个版本文件。
- [x] 新版本除 Agent version 与 Data Access 外，其余契约与 base 精确一致。
- [x] 任意 Product 可显式选择；依赖 Product 不被隐式授权或暴露给 Query Interface。
- [x] MX scope 只接受当前授权的精确 RID 列表；新增 RID 不扩张旧版本，撤销 RID 使依赖版本在运行时阻断。
- [x] 相同重试幂等，并发相同只创建一个版本，并发不同不会丢失或覆盖任何 Manifest。
- [x] 发布失败后 Catalog 字节、Team、Daily Team Set、数据库和 artifacts 完全不变。
- [x] 用于真实测试的 RID 均来自临时用户输入 fixture，不读取或复制仓库真实 RID。

## 可能触点

- `advisor/research/contracts.py`
- `advisor/research/catalog.py`
- `advisor/research/agent_access_publication.py`
- `advisor/research/agents/runner.py`
- `tests/advisor/research/test_catalog.py`
- `tests/advisor/research/test_agent_access_publication.py`
- `tests/advisor/research/test_agent_runner.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/test_catalog.py tests/advisor/research/test_agent_access_publication.py tests/advisor/research/test_agent_runner.py'
```
