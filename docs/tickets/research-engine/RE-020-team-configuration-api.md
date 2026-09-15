---
id: RE-020
status: done
depends_on: [RE-018, RE-019]
adrs: [0011, 0053, 0054, 0055, 0056]
---

# 提供 Team Catalog 与配置 API

## 结果

本地 Web API 以易懂中文提供最新 Agent 目录、版本化 Team 目录、Team Publication 和每日启用控制；接口只调用 RE-018/RE-019 的领域服务，不直接执行研究。

## 范围

- `GET /api/research/agents` 按稳定 Agent ID 返回每个 Agent 的最新版本、中文标题和只读摘要字段，不返回历史版本选择项。
- `GET /api/research/teams` 按稳定 Team ID 返回最新版本、只读历史版本、精确 Agent 引用及当前每日启用版本。
- `POST /api/research/teams` 接受 Team ID、中文名称和 Agent ID 列表；新发布返回 `201`，完全相同的幂等重试返回既有引用和 `created: false`。
- `PUT /api/research/daily-teams/{team_ref}` 独立启用精确版本；`DELETE` 同一路径只取消每日启用，不删除 Team。
- API 在每次目录读取和变更前使用当前 Catalog，不缓存已过期 Agent/Team 版本。
- 将领域校验映射为稳定的 `400`、`404` 或 `409`，返回有限中文错误，不泄露绝对路径、YAML 内容或内部异常堆栈。
- 保持现有 Research Cycle 查询、Market Daily、账本和报告接口行为不变。

## 不包含

- 浏览器页面、Agent/Prompt 编辑、Research Run 启动或报告聚合。
- 登录、用户、角色、权限、远程访问、API Key 或多租户能力。
- Team 删除、归档、覆盖、版本号输入或服务器端草稿 CRUD。

## 验收条件

- [x] Agent 接口自动发现新增 Manifest 和新版本，每个 ID 只暴露最新版本供创建 Team。
- [x] Team 接口稳定返回最新版本、历史版本和每日启用状态，且不提供删除/归档链接。
- [x] 发布 API 完整覆盖成功、幂等重试、非法输入、重复风格、未知 Agent 和原子写入失败。
- [x] 每日启用 API 能替换同 ID 旧版本，也能取消最后一个 Team 形成空集合。
- [x] 所有写接口都不触发 Data Product、Codex、Research Cycle 或报告生成。
- [x] API 测试使用临时 Catalog 与配置文件，不修改仓库真实 Team 或每日配置。

## 可能触点

- `advisor/web/api.py`
- `advisor/research/team_publication.py`
- `advisor/research/daily_teams.py`
- `tests/advisor/test_research_web.py`
- `tests/advisor/test_web_api.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/test_research_web.py tests/advisor/test_web_api.py
```
