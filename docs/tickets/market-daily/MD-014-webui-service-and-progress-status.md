---
id: MD-014
status: complete
depends_on: [MD-011, MD-012, MD-013]
adrs: [0040, 0049, 0050, 0051]
---

# 在 WebUI 展示服务与冷启动进度

## 结果

用户能在现有 WebUI 用易懂中文查看 A Hunter Service Set 健康状态、Market Daily 冷启动/日更进度、失败股票和最近数据日期，而页面加载不会触发任何抓取或服务变更。

## 范围

- 新增只读 API：Service Set 摘要、Market Daily 当前状态、Run 列表、Run 详情和失败项分页。
- Market Daily 状态包含服务是否运行、租约时间、模式、目标日期、总数、成功数、失败数、进度、最近成功更新和下一次计划时间。
- 明确区分 `idle`、`waiting_for_cold_start`、`running`、`partial`、`complete`、`failed` 和来源不可用。
- MX Listener 仅展示只读进程/Chrome/CDP 状态，不展示或泄漏调试 URL、RID 内容或会话信息。
- 在前端增加服务卡片、冷启动进度条、失败股票表和简单排查提示。
- 所有状态接口有有界分页和错误收敛；数据库不可用时返回明确 503，不伪造健康。
- 本 ticket 的状态接口与页面加载不得提交冷启动、启动/停止服务或修改配置；后续受控提交动作由 MD-017 单独覆盖。

## 不包含

- 本 ticket 不包含 WebUI 控制按钮、Chrome 操作、股票结论或 Team 比较；MD-017 新增独立验收的冷启动意图提交按钮。
- 实时 WebSocket 推送；首版轮询只读状态即可。

## 验收条件

- [x] 打开或刷新 WebUI 不新增 Request、Run 或 Provider 调用。
- [x] 冷启动进度、partial 失败项、完成状态和服务离线均有 API 与前端测试。
- [x] 失败项分页稳定、有界，不返回响应正文或内部调试标识。
- [x] MX Listener 状态只读且不改变 Collector、Chrome 或 RID 配置。
- [x] 所有用户可见文案为易懂中文。
- [x] 现有报告、图表、Profile 和 Research 页面回归测试通过。

## 可能触点

- `advisor/web/api.py`
- `frontend/src/`
- `tests/advisor/test_web_api.py`
- `tests/advisor/test_research_web.py`
- `frontend/src/**/*.test.*`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/test_web_api.py \
  tests/advisor/test_research_web.py
(
  cd frontend
  /Users/mac/.local/share/chrome-devtools-mcp/node/bin/node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json
  /Users/mac/.local/share/chrome-devtools-mcp/node/bin/node node_modules/vitest/vitest.mjs run --reporter=dot
)
```
