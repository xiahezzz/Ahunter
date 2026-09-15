---
id: RE-036
status: done
depends_on: [RE-033, RE-034]
adrs: [0092, 0093, 0095, 0097, 0099]
---

# 建立分页报告查询区与详情页

## 结果

Research 页面拥有专门的报告查询区域，按报告发布时间倒序分页，按 Team 跨版本筛选，并能安全阅读 passed/partial 报告或追溯无报告终态。

## 范围

- 建立独立 Research Report Explorer；没有首页报告、Primary Team、默认 Market Team 或 Agent 筛选概念。
- 默认查询 passed 与 partial Records，按 `published_at` 从新到旧；服务端分页驱动翻页，不把全部历史加载到浏览器，也不保留 recent-100 上限。
- 主筛选选择稳定 Team ID 并覆盖其所有版本；可选二级筛选精确 `team@version`；每行始终显示实际版本。
- 状态筛选可加入 blocked、failed、cancelled Records；这些行明确没有 Team Report，只显示有限停止阶段/原因。
- 列表展示 Scope、Subject、origin、requested/boundary/published time、status 和 quality 摘要；Security Subject 可按代码识别，Market Subject 不伪造代码。
- Record detail 对 passed/partial 安全渲染 Markdown Team Report，并显示 exact Team、Scope、Subject、时间、evidence quality、risks、invalidation、partial blocked Insights 和 provenance 摘要。
- Markdown 渲染禁止原始 HTML/script、javascript URL、任意本地文件读取和路径暴露；后端 hash verification 失败时前端显示不可用而非渲染未验证内容。
- 任意 terminal Record 提供“再次研究”，创建新 Request 并跳转/提示新 ID；原记录、Boundary 和报告保持不变。
- 分页、筛选和详情 URL 可刷新恢复，current queue 仍是独立全局区域。

## 不包含

- Agent Finding 浏览器、raw artifact/log viewer、跨 Team 比较/排名、报告编辑/删除、文件下载中心或首页卡片。
- 客户端全文索引、远程分享或让 rerun 复用旧 Snapshot。

## 验收条件

- [ ] 150+ Records fixture 可逐页完整访问，publication-time 排序、stable Team 跨版本和 exact-version 筛选正确。
- [ ] 默认只显示 passed/partial；切换状态后 blocked/failed/cancelled 可见且没有伪报告链接。
- [ ] partial 详情同时展示有效 Insights 与 blocked cause；passed/blocked/failed/cancelled 各有清晰中文表现。
- [ ] 恶意 Markdown、hash mismatch、路径逃逸和缺失 artifact fixture 均不会执行或泄漏内容。
- [ ] rerun 创建新 ID/时间并保留 `rerun_of`，原详情刷新前后完全不变。
- [ ] UI 不提供 Agent filter、Primary Team 或首页报告入口，Team filter 与全局 current queue 互不干扰。

## 可能触点

- `frontend/src/research/ResearchReportExplorer.tsx`
- `frontend/src/research/ResearchRecordDetail.tsx`
- `frontend/src/research/contracts.ts`
- `frontend/src/styles.css`
- `frontend/src/research/ResearchReportExplorer.test.tsx`
- `frontend/src/research/ResearchRecordDetail.test.tsx`
- `tests/advisor/test_research_web.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c 'cd frontend && npm test -- --run && npm run build'
```
