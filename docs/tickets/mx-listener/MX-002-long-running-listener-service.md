---
id: MX-002
status: complete
depends_on: [MX-001]
adrs: [0049, 0058, 0059, 0060, 0061]
---

# 实现常驻 MX Listener Service 与安全恢复

## 结果

一个长时间运行的 Node 24 Service 在用户提供的 Chrome/MX 授权条件出现时自动开始被动监听，条件消失时保持可观测等待，并在不破坏事件 durability 的前提下恢复。

## 范围

- 建立深 `MxListenerService` 模块，唯一拥有 Service Lease、RID watcher、Collector、CDP 循环、媒体 drain、retention maintenance、心跳、状态转换和优雅停止。
- 启动必须先通过现有离线 `scripts/self-test.mjs`；子进程使用固定 Node 24、干净非登录环境和清空的 proxy 变量。失败时不得打开事件接收路径。
- CDP 不可达时进入 `waiting_for_chrome`；CDP 可达但没有精确 MX origin target 时进入 `waiting_for_authorization`，两者都保持进程和心跳，不退出、不操作浏览器。
- target 出现后进入 `connecting`；仅在 `Network.enable` 成功且 CDP WebSocket 保持打开时进入 `listening`。
- 短暂断线继续使用现有 1/2/4/8/30 秒有界退避；恢复后自动继续，不导航或刷新页面。
- RID 配置无效时立即切换为空集合、记录 `config_invalid` 并降级；配置恢复后热加载并恢复健康，不重启进程。
- 单帧 decode、单个媒体任务和 maintenance 错误被隔离并计数；事件库不可写、租约丢失或心跳持久化失败触发 30 秒 drain 后退出。
- 仅在 `listening` 时通过可替换的 sleep-inhibition adapter 阻止空闲系统睡眠；离开该状态立即释放。屏幕关闭、主动睡眠和合盖不被伪装成可继续采集。
- SIGINT/SIGTERM 停止接收新帧，最多等待 30 秒完成已排队事件/媒体工作，更新 `stopping` 并释放租约。
- 将 `scripts/run-collector.mjs` 收敛为同一 Service 的人工入口或由新入口直接替代；任何入口都必须争用同一租约，不保留第二套运行语义。

## 不包含

- 启动 Chrome、打开 MX URL、登录、页面交互、LaunchAgent 安装或 WebUI。
- 从静默时长推断登录失效或连接故障。

## 验收条件

- [x] fake CDP 从不可达、无 target、可连接、断线到恢复时，状态顺序和退避完全确定且进程不因授权缺失退出。
- [x] `listening` 才持有 idle-sleep assertion；等待、重连和停止状态均释放。
- [x] 配置无效失败关闭且可热恢复；空集合不接受事件、不下载媒体，但 Service 仍可观测。
- [x] 单帧与单媒体失败不会中断其他合法事件；数据库/租约/心跳故障必定退出而不伪装健康。
- [x] 两个手工入口或手工入口与 Service 同时启动时，只有租约持有者运行采集工作。
- [x] SIGTERM 下队列 drain、媒体任务、RID watcher、maintenance、CDP client、sleep assertion 和数据库均按顺序关闭。
- [x] 全流程没有导航、刷新、点击、输入、注入、Cookie/令牌读取或敏感日志。

## 可能触点

- `src/services/mx-listener-service.mjs`
- `src/services/sleep-inhibition.mjs`
- `src/ingestion/collector-runner.mjs`
- `src/ingestion/collector.mjs`
- `scripts/run-collector.mjs`
- `scripts/run-mx-listener-service.mjs`
- `tests/unit/mx-listener-service.test.mjs`
- `tests/unit/collector-runner.test.mjs`
- `tests/integration/mx-listener-recovery.test.mjs`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/mx-listener-service.test.mjs tests/unit/collector-runner.test.mjs tests/integration/mx-listener-recovery.test.mjs'
```
