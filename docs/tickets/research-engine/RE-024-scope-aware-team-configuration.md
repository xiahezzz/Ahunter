---
id: RE-024
status: done
depends_on: [RE-020, RE-021, RE-023]
adrs: [0053, 0054, 0055, 0070, 0077, 0089]
---

# 让 Team 配置感知 Research Scope

## 结果

用户在现有 Team 配置区先选择 Research Scope，再从同 Scope Agent 中创建或修订 Team；后端始终重新校验，既有历史版本和统一发布语义保持不变。

## 范围

- Agent 与 Team Catalog API 返回 Scope，并继续返回精确版本、latest 标识、中文标题和不可变成员引用。
- 新建 Team Draft 时先选择 Scope，Agent 选择器只展示兼容身份；修订 Team 时稳定 Team ID 与 Scope 均锁定，只允许修改名称和成员。
- Team Publication 在服务端重新解析当前最新 Agent 版本并验证 Scope，同一 Team 的修订不能借由 Agent 升级改变 Scope。
- 新发布 Team Manifest 显式写 Scope；不同 Scope 即使成员输入或标题相同也不能绕过稳定身份约束。
- 现有七个 Agent、`a_share_core` 与 `normal` 在界面中显示为 Security；RE-032 新增的 Agent 与 Team 将自然显示为 Market，无前端硬编码名单。
- 保留历史 Team 版本的只读目录，使后续启动研究能选择任意精确已发布版本。

## 不包含

- 创建或编辑 Agent Scope、删除 Team、保存持久化草稿、运行研究或配置新的 Market Agent。
- 第二套 Market Team 页面、权限系统或基于角色的产品白名单。

## 验收条件

- [ ] 新建 Team 必须先选择 Scope，UI 不允许选中异 Scope Agent，直接构造的恶意 API 请求也被后端拒绝。
- [ ] 修订时 Team ID 与 Scope 只读；发布下一版本不会修改任何既有 Manifest 或自动升级既有 Team 引用。
- [ ] 旧无 Scope Manifest 在 UI/API 中稳定显示为 Security，且不会因浏览或发布其他 Team 被重写。
- [ ] Catalog API 能列出一个稳定 Team 的全部精确版本，前端不只保留 latest。
- [ ] Team 配置现有发布、幂等双击、空成员和跨稳定 ID 重复成员校验全部回归通过。

## 可能触点

- `advisor/research/team_publication.py`
- `advisor/research/agent_manifest_publication.py`
- `advisor/web/api.py`
- `frontend/src/research/contracts.ts`
- `frontend/src/research/TeamsPanel.tsx`
- `frontend/src/research/ResearchConfigurationPage.tsx`
- `tests/advisor/test_research_web.py`
- `tests/advisor/research/test_team_publication.py`
- `frontend/src/research/TeamsPanel.test.tsx`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/test_team_publication.py tests/advisor/test_research_web.py && cd frontend && npm test -- --run'
```
