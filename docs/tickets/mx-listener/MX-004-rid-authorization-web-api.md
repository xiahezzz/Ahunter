---
id: MX-004
status: complete
depends_on: []
adrs: [0062, 0063]
---

# 建立 RID Authorization Set 深模块与 Web API

## 结果

本地 WebUI 可通过一个窄、原子、并发安全的接口读取和替换唯一 RID Authorization Set，而 Listener 与 Research Engine 继续消费同一个权威 YAML。

## 范围

- 定义统一 RID 配置契约：仅允许唯一的正安全整数列表，最多 1,000 项；空列表合法并表示故意停用。
- 让 Node Loader、Python Research Adapter 和新 Web 配置模块共享同一组契约 fixture，消除当前重复、上限和唯一性校验差异。
- 建立深 `RidAuthorizationStore` 模块，封装安全读取、版本计算、校验、锁、同目录临时文件、fsync 和原子 replace；API 与测试不直接写 YAML。
- 配置版本基于实际已读取文件字节的 SHA-256；替换必须携带调用者观察到的版本，版本不符返回稳定 conflict，避免覆盖手工编辑。
- 拒绝 symlink、非普通文件、过大文件、额外 YAML key、重复 RID、浮点数、布尔值、负数、零和超出安全整数范围。
- 成功写入使用唯一、排序后的规范 YAML；失败前后权威文件字节完全不变，且不创建 Advisor 数据库副本。
- 提供 `GET /api/mx/rids` 与 `PUT /api/mx/rids`：返回当前集合、版本和 collection enabled；写接口只接受严格 JSON 与精确字段。
- 本地 mutation 请求执行同源/Content-Type 检查并返回有限中文错误；不增加用户、角色、API Key、远程访问或多租户。
- 移除 RID 只改变未来授权，不删除事件、媒体、Research Artifacts 或 Agent Manifests。

## 不包含

- RID 发现、流量建议、批量导入推断、事件删除、Agent Feed 分配或 WebUI。
- 修改仓库真实 `config/allowed-rids.yaml`；全部测试使用临时权威路径。

## 验收条件

- [x] Node、Python 与 API 对同一合法/非法 fixture 得出相同结果。
- [x] GET 不创建文件或数据库；PUT 成功后 Listener watcher 能在一个轮询周期内读取完整新集合，不观察到半写文件。
- [x] 两个持有同一旧版本的并发 PUT 只有一个成功，另一个返回 conflict 且不覆盖新内容。
- [x] 原子 replace、fsync 或权限失败保持原文件不变并返回有限 503。
- [x] 空集合合法；未知/重复/非整数 RID 被拒绝，系统永不推断或建议 RID。
- [x] 移除 RID 后历史事件和媒体数量不变。
- [x] API 错误不包含绝对路径、YAML 正文、临时文件名或异常堆栈。

## 可能触点

- `src/config/load-allowed-rids.mjs`
- `src/config/watch-allowed-rids.mjs`
- `advisor/mx/rid_authorization.py`
- `advisor/web/api.py`
- `tests/fixtures/rid-authorization/`
- `tests/unit/load-allowed-rids.test.mjs`
- `tests/advisor/test_mx_rid_authorization.py`
- `tests/advisor/test_mx_web.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/load-allowed-rids.test.mjs'
```

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/test_mx_rid_authorization.py tests/advisor/test_mx_web.py'
```
