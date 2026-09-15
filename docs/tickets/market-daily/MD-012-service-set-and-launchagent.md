---
id: MD-012
status: complete
depends_on: [MD-011]
adrs: [0047, 0048, 0049, 0051]
---

# 建立 Service Set 与 LaunchAgent 能力

## 结果

A Hunter 提供统一的服务发现和运维命令，Market Daily 由独立 KeepAlive LaunchAgent 托管；MX Listener 本轮仅进入状态面，不改变其人工启动和授权流程。

## 范围

- 定义 Service Descriptor、状态、日志位置和统一 `advisor-services` CLI。
- 注册 Market Daily 与 MX Listener 两个独立服务；运行时、数据库和故障域保持分离。
- 为 Market Daily 新增 KeepAlive LaunchAgent 模板，调用 `.venv-runtime/bin/advisor-market-daily service run`。
- LaunchAgent 不使用 `StartCalendarInterval`，21:00 计时完全属于 Market Daily Service。
- 扩展模板校验、渲染、安装、加载、卸载、启动、停止和状态查询，操作幂等且目标路径明确。
- Market Daily 日志写入项目 `logs/`，Service Manager 能定位最新日志但不回传无限内容。
- MX Listener 只实现进程/Chrome/CDP 的只读状态探测；不得自动启动 Chrome、Collector、页面或修改 RID。
- 继续使用 `.venv-runtime` 作为实际服务环境，不让开发环境承担生产常驻进程。

## 不包含

- WebUI、真实安装执行、MX Listener 自动恢复或 Chrome 操作。

## 验收条件

- [x] Market Daily plist 为 KeepAlive 且没有 21:00 日历字段。
- [x] 模板不存在 shell 插值，固定使用仓库根目录、runtime Python 和项目日志路径。
- [x] render/install/load/unload 重复执行保持幂等，错误目标被拒绝。
- [x] Service Manager 分别报告两个服务，任一失败不改变另一服务状态。
- [x] MX Listener 状态检查完全只读，不导航、刷新、点击、输入或注入页面。
- [x] 现有 API、frontend、premarket 和 review LaunchAgent 测试继续通过。

## 可能触点

- `advisor/services/contracts.py`
- `advisor/services/manager.py`
- `advisor/services/cli.py`
- `advisor/scheduler/launchd.py`
- `config/launchd/com.ahunter.market-daily.plist.template`
- `tests/advisor/test_services.py`
- `tests/advisor/test_launchd.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/test_services.py \
  tests/advisor/test_launchd.py
```
