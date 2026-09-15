---
id: MX-007
status: complete
depends_on: [MX-005, MX-006]
adrs: [0063, 0066]
---

# 实现用户友好的 MX 历史资讯流

## 结果

用户可从 MX 顶层入口快速浏览、检索和查看全部 Accepted MX Events 与安全媒体，同时保留阅读位置并清楚区分当前授权和历史 RID。

## 范围

- 在 MX 监听页面加入 Information View；默认按接收时间倒序显示 50 条用户友好资讯卡片。
- 卡片展示 RID、当前授权状态、来源/接收时间、规范化正文、媒体缩略图和有限追溯信息；图片-only 事件不能被误报为空。
- 组合筛选支持一个或多个 RID、当前授权/已撤销授权、接收时间范围、有无图片和规范化正文关键词。
- 筛选状态编码进 URL；刷新、复制本地链接和前进/后退可恢复同一视图，非法 URL 参数被安全重置并提示。
- 使用 API opaque cursor 加载更早页面；追加新事件不打乱已加载内容，不实现 offset 分页。
- 仅位于第一页且页面可见时每 5 秒检查 head；发现更新显示“发现 N 条新资讯”，用户点击后才刷新，不自动插入或移动滚动位置。
- 点击卡片打开详情，按 text/media blocks 用户友好排版；媒体通过 opaque endpoint 加载，可放大查看但不显示本地路径或来源 URL。
- 已撤销 RID 使用明确、非危险的“历史 RID”标记，仍可筛选和查看；不提供恢复授权或删除的隐式快捷操作。
- loading、空结果、游标失效、数据库不可用、媒体失败和刷新失败各自有有限中文状态；旧成功页面不能在刷新失败后伪装为当前结果。
- 不对事件生成 AI 摘要、情绪标签、股票建议或自动分类；页面忠实展示规范化事实。

## 不包含

- 原始载荷/JSON 调试器、事件或媒体删除、批量导出、远程分享、WebSocket/SSE 或 Agent 配置。
- 将当前 RID 筛选误用为历史数据授权或 Research Snapshot 选择。

## 验收条件

- [x] 用户能从概览一键到达 MX 资讯，查看文本、图片-only 和多图片事件。
- [x] 五类筛选可任意组合并在 URL/刷新后保持；页面只请求有界数据。
- [x] 游标连续加载无重复/遗漏，新数据到达不移动正在阅读的旧列表。
- [x] 5 秒检查只在可见第一页发生；新资讯必须点击后插入，离开页面时请求与 timer 被取消。
- [x] 当前/历史 RID 标签与 API 一致，移除 RID 不让已显示历史消失。
- [x] DOM、网络响应快照和错误信息均不含 raw payload、source URL、本地路径、令牌或调试标识。
- [x] 键盘、窄屏、空结果、断网、媒体 404 和 malformed API 响应均有自动化覆盖。

## 可能触点

- `frontend/src/mx/MxInformationFeed.tsx`
- `frontend/src/mx/MxEventDetail.tsx`
- `frontend/src/mx/contracts.ts`
- `frontend/src/mx/*.test.tsx`
- `frontend/src/styles.css`
- `frontend/src/App.test.tsx`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node frontend/node_modules/vitest/vitest.mjs run --root frontend src/mx --reporter=dot'
```

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node frontend/node_modules/typescript/bin/tsc --noEmit -p frontend/tsconfig.json'
```
