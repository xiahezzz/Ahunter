---
id: MX-003
status: complete
depends_on: [MX-002]
adrs: [0048, 0049, 0051, 0058, 0060, 0061]
---

# 纳入 LaunchAgent 与统一 Service Set 生命周期

## 结果

MX Listener 由独立用户级 KeepAlive LaunchAgent 托管，并与 Market Daily 一样通过统一 CLI/API-ready Manager 安装、启动、停止和查看状态，同时保持 Node/Python、数据库和故障域隔离。

## 范围

- 新增 `com.ahunter.mx-listener.plist.template`，固定使用仓库要求的 Node 24、项目工作目录、9333 loopback CDP 参数和项目日志路径。
- 使用用户 GUI domain LaunchAgent、`RunAtLoad`/`KeepAlive` 和明确 `ThrottleInterval`；不使用 shell 插值、日历触发或常驻 `caffeinate` wrapper。
- LaunchAgent 只启动 Listener Service；不得启动 Chrome、打开 URL、管理 profile、执行登录或读取浏览器凭据。
- 扩展 launchd 模板校验和渲染，拒绝其他 Node、非 loopback CDP、缺失日志、shell 命令和敏感环境变量。
- 将 `MX_LISTENER.mutable` 改为 true；统一 `advisor-services install/load/unload/start/stop/status mx-listener`，并保持幂等。
- Service Manager 以 MX 事件库控制面为状态权威，报告 liveness/readiness/health、活动时间、配置是否为空和有限 reason；不再用 `pgrep + CDP` 猜测连接完成状态。
- `stop` 的语义是卸载 MX LaunchAgent并等待 lease 释放；`start` 安装/加载并 kickstart，但不等待浏览器授权。
- 公开有限日志路径 `logs/mx-listener.out.log` 与 `logs/mx-listener.err.log`，不得通过状态接口回传无界日志正文。
- 保持 Market Daily、Advisor API/frontend 和定时任务的模板、安装与状态行为不变。

## 不包含

- 实机写入 `~/Library/LaunchAgents`、启动真实 Listener、WebUI 或 Chrome 操作；实机动作属于 MX-012。
- 合并 Node 与 Python 服务进程或数据库。

## 验收条件

- [x] MX plist 只调用固定 Node 24 Service 入口，具备 KeepAlive/ThrottleInterval，且没有页面或 Chrome 启动参数。
- [x] render/install/load/unload/start/stop 对 MX 幂等，永远只命中精确的 `com.ahunter.mx-listener` target。
- [x] Manager 从事件库返回三维状态；陈旧心跳显示离线，静默事件不显示异常。
- [x] MX 与 Market Daily 可独立加载、停止和失败，任一操作不改变另一服务。
- [x] 状态与错误不暴露 CDP URL、MX URL、RID、绝对配置内容或载荷。
- [x] 所有现有 LaunchAgent 和 Service Set 回归测试继续通过。

## 可能触点

- `config/launchd/com.ahunter.mx-listener.plist.template`
- `advisor/scheduler/launchd.py`
- `advisor/services/contracts.py`
- `advisor/services/manager.py`
- `advisor/services/cli.py`
- `tests/advisor/test_launchd.py`
- `tests/advisor/test_services.py`
- `tests/advisor/test_mx_listener_services.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/test_launchd.py tests/advisor/test_services.py tests/advisor/test_mx_listener_services.py'
```
