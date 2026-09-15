---
id: RE-021
status: done
depends_on: [RE-020]
adrs: [0003, 0053, 0055, 0056]
---

# 在 WebUI 配置 Research Teams

## 结果

用户可以在现有本地 WebUI 用中文创建、修订、查看和每日启用 Research Team；页面只负责收集意图和刷新状态，不直接写文件或调用 Codex。

## 范围

- 新增“研究团队”区域，加载最新 Agent 卡片和按稳定 ID 分组的 Team 卡片。
- 新建表单要求手工填写小写下划线 Team ID、中文名称并勾选至少一个 Agent；不显示历史 Agent 版本选择，也不提示证据面过窄。
- 草稿只存在当前页面内存；取消、离开或刷新即丢弃，不写 local storage、SQLite 或草稿文件。
- “发布 Team”调用 Publication API，提交期间禁止重复点击；成功后显示精确 `team@version` 并刷新目录，但不自动每日启用。
- “基于此版本新建”沿用并锁定 Team ID，载入名称和 Agent 身份；发布时由后端解析最新 Agent 版本并自动生成下一 Team 版本。
- Team 卡片默认展示最新版本，并以折叠区只读展示历史版本；不提供覆盖、删除或归档按钮。
- 每个版本提供独立“每日启用”操作；当前启用版本提供“取消每日启用”，启用新版本后如实展示它已替换同 ID 的旧版本。
- Daily Team Set 为空时页面正常展示“当前未启用每日 Team”，不渲染为错误状态。
- 所有成功、校验失败和刷新失败反馈使用有限、易懂中文，并区分“操作已成功但刷新失败”。

## 不包含

- Agent Manifest、instructions、Data Product、模型或 Decision Pipeline 编辑器。
- Research Run 启动页面、Team 结论比较或跨 Team 今日总结。
- 多用户协作、权限、审批、远程同步或通用插件管理界面。

## 验收条件

- [x] 新建与修订流程只提交 Agent ID，用户无法输入 Team/Agent 版本号。
- [x] 空成员在浏览器侧被拒绝，但任意非空组合均可提交且没有证据覆盖警告。
- [x] 发布成功不改变每日状态；每日启用是清晰独立的按钮。
- [x] 启用新版本替换旧版本、取消最后一个版本形成空集合，页面状态与 API 一致。
- [x] 刷新会丢弃未发布草稿，但不会改变已发布 Manifest 或每日集合。
- [x] 前端测试覆盖加载、发布、幂等响应、修订、历史折叠、启用、替换、取消和错误反馈。

## 可能触点

- `frontend/src/App.tsx`
- `frontend/src/styles.css`
- `frontend/src/App.test.tsx`
- `frontend/src/research/TeamsPanel.tsx`

## 验证

```bash
cd frontend && npm test -- --run && npm run build
```
