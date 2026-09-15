---
id: RE-027
status: done
depends_on: [RE-026, MD-012]
adrs: [0049, 0050, 0073, 0074, 0090]
---

# 统一 Research 触发入口与服务生命周期

## 结果

CLI、08:30 scheduler 和本机 Service Set 只提交或托管 Research Service，不再各自直接执行 Research Cycle；服务健康与离线状态可被统一读取。

## 范围

- 提供 Research Service 进程入口、Service Lease/heartbeat/status 快照、SIGINT/SIGTERM 安全退出和有限结构化日志。
- 将 Research Service 纳入现有 A Hunter Service Set 与 `.venv-runtime`，提供 repository-owned LaunchAgent 渲染、安装前校验、显式 start/stop/status；不得隐式安装或加载用户 LaunchAgent。
- CLI 手动研究命令改为提交 origin=`cli` 的 durable Request 并立即返回 ID；可选等待行为只能轮询控制面，不能回退为 CLI 内直接执行。
- 08:30 scheduler 改为提交 origin=`scheduled` 的 Requests；Security Team 仍按明确候选代码逐 Subject 提交，Market Team 每次调度 occurrence 提交全市场 Subject，不设置每日唯一键。
- WebUI/API 进程重启、CLI 退出或 scheduler 退出均不改变已提交 Request 的真实状态。
- Service status 区分 offline、starting、idle、running、stopping、degraded/failed，并只暴露有限 reason code、active Request ID、heartbeat 和队列计数。
- 保留现有 Market Daily、MX Listener 独立运行时和失败域；Research Service 不启动 Chrome、不管理 MX 授权、不执行 Market Daily ingestion。

## 不包含

- Web API/WebUI 研究按钮、Market Providers、真实 LaunchAgent 安装或真实 Codex 运行。
- 定时计划编辑器、每天一次限制、外部任务队列或新的本地监听端口。

## 验收条件

- [ ] CLI 与 scheduler 测试证明只写 Request，不调用 Provider、Codex 或 State Machine。
- [ ] scheduler 同一天多次 occurrence 可产生多条 Request；传输重试仍按同一 submission identity 幂等。
- [ ] Service Set status 能真实区分离线、空闲和运行，陈旧 heartbeat 不能伪装在线。
- [ ] 两个 LaunchAgent/手动实例不会双持租约；SIGTERM 不再认领新工作并保留队列。
- [ ] 渲染的 LaunchAgent 使用 `.venv-runtime`，不会引用 `.venv311`、用户凭据或 Codex session material。
- [ ] 现有 MX Listener 与 Market Daily Service 生命周期测试全部回归通过。

## 可能触点

- `advisor/research/cli.py`
- `advisor/research/service.py`
- `advisor/scheduler/premarket.py`
- `advisor/services/manager.py`
- `advisor/scheduler/launchd.py`
- `pyproject.toml`
- `tests/advisor/research/test_cli.py`
- `tests/advisor/test_services.py`
- `tests/advisor/test_launchd.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/test_cli.py tests/advisor/test_services.py tests/advisor/test_launchd.py tests/advisor/test_research_entry.py'
```
