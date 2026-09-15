---
id: MX-006
status: complete
depends_on: [MX-003, MX-004]
adrs: [0049, 0058, 0060, 0062, 0068]
---

# 建立顶层导航与 MX 运维 WebUI

## 结果

WebUI 提供“概览 / MX 监听 / 研究配置”三个稳定入口；用户可在 MX 页面查看真实服务状态、显式启动隔离的专用 Chrome、受控启停 Listener，并安全配置 RID Authorization Set，而不会自动操作 Chrome 或 MX 页面。

## 范围

- 将现有长单页拆为三个路由级页面：概览保留现有账户/行情/报告摘要，MX 监听页面承载 Listener 运维，研究配置页承载既有 Team 面板并为 MX-011 预留 Agent Access 区域。
- 顶栏提供可键盘访问的中文导航，浏览器前进/后退、直接打开路径和刷新均保持当前页面；不把草稿偷偷写入 localStorage。
- 概览的 MX Service 卡显示 liveness/readiness/health 摘要并提供“查看 MX 资讯”快捷入口。
- MX 页面分别展示：LaunchAgent loaded、心跳/租约、Readiness、Health、当前连接时长、最后 frame、最后 accepted event、RID 数量和 collection enabled。
- `waiting_for_chrome` 提供独立的“启动专用 Chrome”按钮；它只启动固定本地应用、固定 loopback 调试端点和隔离 profile，不接收 URL 或设置。`waiting_for_authorization` 继续提示用户自行登录和打开页面。
- 专用 Chrome 启动与 Listener 生命周期是两个接口；Listener 启停、LaunchAgent 和重连不得隐式调用浏览器启动器。启动器不导航、登录、刷新、点击、输入、注入或读取页面。
- 增加受控 `start/stop mx-listener` Web API 与按钮；只调用统一 ServiceSetManager，操作成功后刷新状态。停止不关闭 Chrome，不改变 RID，不删除数据。
- RID 编辑器读取当前集合和版本，允许逐项新增/移除正整数；提交完整集合，显示空集合会停用采集、移除不会删除历史的确认文案。
- 版本 conflict 保留用户输入并提示刷新比较；成功写入后显示 Listener 将热加载，不虚构已生效状态。
- 状态每 5 秒只读轮询，页面卸载时取消；轮询失败只降级 MX 页面，不让概览、Research、账本或报告失效。
- 所有 mutation 严格使用本地同源 JSON 接口、禁用重复提交，并区分“操作成功但状态刷新失败”。

## 不包含

- 历史资讯列表与媒体浏览（MX-007）、Agent Access 编辑（MX-011）、Chrome 页面自动化、RID 建议或事件删除。
- 实机安装 LaunchAgent；按钮测试使用 fake Manager 与临时 LaunchAgents 目录。

## 验收条件

- [x] 三个顶层入口可直接访问、刷新和前进/后退，现有概览与 Team 行为保持不变。
- [x] 三维状态不会压成单一“正常/异常”；静默只显示活动时间，不触发告警。
- [x] 专用 Chrome 只能由本地 WebUI 显式启动，重复请求幂等；API/UI 不返回端口、profile、PID 或调试标识。
- [x] 等待 Chrome/授权时给出人工步骤，零页面导航、登录、刷新、点击、输入、注入或内容读取调用。
- [x] start/stop 只命中 MX LaunchAgent；重复操作幂等，不改变 Market Daily、Chrome、RID 或历史数据。
- [x] RID 新增、移除、空集合、非法输入、并发 conflict、写入失败和成功热加载提示均有前后端测试。
- [x] 从真实配置读取的 RID 只在本地 UI/API 显示，不进入日志、测试快照或错误正文。
- [x] 页面在窄屏、键盘导航和 loading/error/empty 状态下仍可用，所有文案为易懂中文。

## 可能触点

- `advisor/web/api.py`
- `advisor/mx/chrome_launcher.py`
- `advisor/services/manager.py`
- `frontend/src/App.tsx`
- `frontend/src/navigation/`
- `frontend/src/mx/MxOperationsPage.tsx`
- `frontend/src/research/`
- `frontend/src/styles.css`
- `tests/advisor/test_mx_web.py`
- `tests/advisor/test_chrome_launcher.py`
- `frontend/src/mx/MxOperationsPage.test.tsx`
- `frontend/src/App.test.tsx`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/test_chrome_launcher.py tests/advisor/test_mx_web.py tests/advisor/test_services.py'
```

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node frontend/node_modules/vitest/vitest.mjs run --root frontend src/mx/MxOperationsPage.test.tsx src/App.test.tsx --reporter=dot'
```
