---
id: MD-017
status: complete
depends_on: [MD-002, MD-014]
adrs: [0040, 0050, 0052]
---

# 在 WebUI 提交五年冷启动

## 结果

用户可以在本地 WebUI 的 Market Daily 卡片点击“启动五年同步”，提交一次可恢复、幂等的冷启动意图；常驻 Market Daily Service 仍是唯一实际执行者。

## 范围

- 新增无参数 `POST /api/market-daily/cold-start`，只调用 `submit_cold_start_intent` 并返回 `202 Accepted`、Request 编号、Request 状态和中文说明。
- 接口不联系行情来源、不冻结日期、不创建 Run、不启动或停止服务；重复请求返回同一个持久化 Request。
- WebUI 仅在 `idle` 显示“启动五年同步”按钮，明确说明点击只写入本地队列、服务将在 21:00 后执行。
- 成功后刷新状态并显示“已排队”；接口失败时展示有界中文错误，不伪造成功。
- 页面加载、刷新和其他状态接口继续只读。

## 不包含

- 从浏览器直接抓取、重试或修改行情数据。
- 任意日期、证券范围或数据源参数。
- 从 WebUI 启动/停止 LaunchAgent、Market Daily Service 或 MX Listener。

## 验收条件

- [x] API 只提交同一个无日期幂等 Request，重复调用不增加队列项，也不调用 Provider。
- [x] `idle` 状态显示启动按钮；提交后状态变为已排队，按钮不再重复提交。
- [x] 提交失败有明确、有限的中文反馈；状态刷新失败不伪称已刷新。
- [x] Python API 测试、前端交互测试、完整离线回归和格式检查通过。

## 可能触点

- `advisor/web/api.py`
- `frontend/src/App.tsx`
- `frontend/src/styles.css`
- `tests/advisor/test_market_daily_web.py`
- `frontend/src/App.test.tsx`
- `docs/adr/0052-allow-local-webui-to-queue-idempotent-cold-start.md`
- `docs/market-daily-operations.md`

## 验证

运行定向 Python/前端测试，再运行完整离线回归与 `git diff --check`。

## 验收记录

### 2026-08-07：完成

- API 测试连续两次 `POST /api/market-daily/cold-start` 只得到同一个 `mdreq-c2c28608551dc2c7684c7cee`；队列中仅有一条 pending Request，且未创建 Run 或行情行。
- 前端测试覆盖 `idle` 按钮、成功后刷新为“冷启动请求已排队”、接口拒绝提示和“请求已写入但状态刷新失败”的如实提示。
- 本机当前 API 的真实幂等 POST 返回 `202 Accepted` 与上述已存在的 Request；数据库仍为 pending、Run 数为 0，服务不会因此提前抓取。
- 完整验证：Python 592 通过（仅保留既有 httpx/Starlette 弃用警告）、前端 TypeScript 与 56 个测试通过、Node 自检 134 通过。
