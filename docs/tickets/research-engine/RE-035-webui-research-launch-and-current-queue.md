---
id: RE-035
status: done
depends_on: [RE-024, RE-034]
adrs: [0072, 0080, 0090, 0091, 0098, 0099]
---

# 在 WebUI 启动研究并查看全局队列

## 结果

用户能在 Research 页面立即触发任意精确已发布 Team 版本，并在同页看到全系统唯一 running 研究、全部 queued 请求及阶段进度。

## 范围

- 增加“立即研究”区域，按稳定 Team 展开可选精确版本并显示 Scope、标题、版本和成员摘要；历史版本可选，draft 不可选。
- 选择 Market Team 时不显示/不提交股票代码；选择 Security Team 时显示一个六位代码输入并在前后端同时验证。
- 每次点击只创建一个 Team/一个 Subject/一个 Request，成功后立即显示 Request ID 与 queued 状态，不等待研究完成。
- 不设置每日次数提示、禁用或 Team/date 去重；仅在同一次提交 pending 时防止重复 transport click。
- 增加全局“当前研究”区域：顶部唯一 running，随后按 API 顺序展示全部 queued；不继承 Team 或报告筛选。
- 每行显示 exact Team、Scope、Subject、origin、requested/boundary time、phase、Agent x/y、Decision Stage、last update 和取消按钮。
- queued/running 取消需要明确操作反馈；已进入终态后刷新为真实结果，不在客户端猜测取消成功。
- 首版使用有界 polling；Service offline/degraded、API 503 和陈旧状态均以易懂中文显示，页面不伪造预计完成时间。
- Research 页面保留 Team 配置入口，但不创建首页、默认 Team 或 Market Overview 特权区域。

## 不包含

- 报告分页与详情渲染（RE-036）、WebSocket、批量股票输入、运行时选 Agent、自动 child research 或浏览器执行 Codex。
- Team 删除、远程共享、权限或通用任务调度编辑器。

## 验收条件

- [ ] Market/ Security Team 切换时输入形状和中文提示正确，非法代码或跨 Scope payload 无法提交。
- [ ] 任意精确历史 Team 版本可提交；提交后 UI 立即出现 queued Record 且没有长 HTTP 请求。
- [ ] 多条同日同 Team 请求全部可见；current queue 顺序与 API 一致且不受页面筛选影响。
- [ ] 各进度阶段、partial/blocked/failed/cancelled 终态和 Service offline 都有前端 fixture 测试。
- [ ] queued/running cancel 行为、重复点击和请求失败不会导致幽灵记录或误报成功。
- [ ] 页面加载和轮询均为只读，绝不自动提交 Research Request。

## 可能触点

- `frontend/src/research/ResearchConfigurationPage.tsx`
- `frontend/src/research/ResearchLauncher.tsx`
- `frontend/src/research/CurrentResearchQueue.tsx`
- `frontend/src/research/contracts.ts`
- `frontend/src/styles.css`
- `frontend/src/research/ResearchLauncher.test.tsx`
- `frontend/src/research/CurrentResearchQueue.test.tsx`
- `tests/advisor/test_research_web.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c 'cd frontend && npm test -- --run && npm run build'
```
