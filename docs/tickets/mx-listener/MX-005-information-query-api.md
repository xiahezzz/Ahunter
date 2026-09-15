---
id: MX-005
status: complete
depends_on: [MX-001, MX-004]
adrs: [0059, 0063, 0066]
---

# 建立 MX Information View 查询、检索与媒体 API

## 结果

本地 Web API 能稳定分页、筛选、全文检索和展示全部 Accepted MX Events，并以安全、不泄漏文件路径或来源 URL 的方式提供已下载媒体。

## 范围

- 建立深 `MxInformationStore` 模块，封装只读打开事件库、Schema 校验、查询计划、游标、详情规范化和媒体 descriptor；Web 路由不直接拼 SQL、解析任意 JSON 或打开任意路径。
- 读取路径拒绝 symlink、非普通文件、缺失/未知 Schema 和写权限升级；数据库不存在时不创建，使用 WAL-compatible/query-only 连接。
- 为 `decoded_text` 建立 FTS5 索引与幂等 backfill/trigger，索引只含安全规范化文本，不含 raw payload、source URL 或 `parsed_content_json` 原文。
- 提供 `GET /api/mx/events`，默认 50、最大 100，按 `(received_at, event_id)` 倒序 keyset pagination；游标不可伪造并绑定当前筛选条件。
- 支持组合筛选：明确 RID 集合、当前授权/已撤销授权、接收时间范围、有无媒体和有界关键词全文检索；所有参数化查询均有扫描与响应大小上限。
- 每个列表项返回 opaque event ID、RID、当前授权标记、来源/接收时间、用户友好正文摘要、媒体摘要和内容哈希；不返回 raw payload、source message ID、OID、原始媒体 URL 或本地路径。
- 提供 `GET /api/mx/events/{event_id}`，把已验证的解析结果转换为有限 text/media blocks；未知结构降级为安全正文，不把任意嵌套 JSON 直接交给浏览器。
- 提供 `GET /api/mx/events/{event_id}/media/{media_id}`：先验证 DB 关联，再以 `O_NOFOLLOW` 打开 `data/media` 内普通文件，校验 inode、大小和允许的图片 MIME 后流式返回。
- 当前已撤销 RID 的历史事件继续可查询并明确标记；RID 删除不会改变游标或历史行。
- API 返回有限中文 400/404/409/503；数据库、FTS 或媒体异常不泄漏 SQL、绝对路径或异常堆栈。

## 不包含

- 原始载荷浏览、原始媒体 URL、下载任意本地文件、事件编辑/删除、AI 摘要或研究结论。
- WebUI、实时 WebSocket/SSE 或对真实事件库执行 backfill；实机迁移属于 MX-012。

## 验收条件

- [x] 新事件持续写入时，keyset 翻页无重复、无跳项；向前追加数据不改变既有下一页结果。
- [x] RID、授权状态、时间、媒体和关键词筛选可组合，游标不能跨筛选条件复用。
- [x] FTS backfill 对旧库幂等，新插入事件自动可搜；索引不含 raw payload 或被移除的免责声明独立文本。
- [x] 已撤销 RID 历史可见并标记，当前配置变化不删除或重写事件。
- [x] 详情响应只含允许字段；深层、超长、畸形 `parsed_content_json` 失败关闭或安全降级。
- [x] 媒体接口拒绝路径穿越、symlink、替换竞态、非图片、过大文件和不属于事件的 media ID。
- [x] 所有列表、详情和错误响应均不包含原始载荷、来源 URL、本地路径、Cookie、令牌或调试标识。

## 可能触点

- `advisor/mx/information.py`
- `advisor/mx/media.py`
- `advisor/web/api.py`
- `src/events/schema.sql`
- `src/events/event-store.mjs`
- `tests/advisor/test_mx_information.py`
- `tests/advisor/test_mx_media.py`
- `tests/advisor/test_mx_web.py`
- `tests/integration/mx-search-index.test.mjs`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/integration/mx-search-index.test.mjs'
```

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/test_mx_information.py tests/advisor/test_mx_media.py tests/advisor/test_mx_web.py'
```
