---
id: MD-011
status: complete
depends_on: [MD-002, MD-009, MD-010]
adrs: [0040, 0047, 0048, 0050]
---

# 实现常驻 Market Daily Service

## 结果

一个全天常驻的 Python Service 独占执行 SQLite 中的 Market Daily Requests，在 21:00 自动运行到期工作，并在崩溃或重启后从持久化边界恢复。

## 范围

- 建立 `MarketDailyService` 深接口，唯一拥有 Request 认领、冷启动、Catch-up、内部 21:00 调度和 Service Lease。
- 启动时恢复过期租约与未完成 Run；若没有冷启动基线则只等待显式冷启动请求，不自行创建。
- 每天 21:00（Asia/Shanghai）检查到期工作：partial 冷启动优先恢复，完成后才运行 Catch-up。
- 空闲时低开销等待；使用可注入时钟和有界轮询，不忙等、不新开本地端口。
- SIGINT/SIGTERM 停止认领新股票，完成或安全回滚当前短事务，保存状态后退出。
- 提供 CLI：`service run`、`cold-start`、`status`；`cold-start` 只提交 SQLite Request 并立即返回 ID。
- 使用数据库租约阻止 CLI、两个 Service 实例或重启重叠造成重复抓取。
- 结构化日志只输出 Run ID、计数、阶段和有限错误，不输出响应正文。

## 不包含

- LaunchAgent 安装、WebUI、MX Listener 生命周期或真实冷启动。

## 验收条件

- [x] fake clock 跨过 21:00 时恰好触发一次，到期前不触发。
- [x] 无冷启动请求时 Service 常驻但不抓取；提交后在一个轮询周期内认领。
- [x] 同时启动两个实例时只有一个持有租约，另一个明确退出或待命。
- [x] 21:00 遇到 partial 冷启动时恢复原 Run，不创建 Catch-up。
- [x] SIGTERM 与强制崩溃恢复测试均不产生重复行或丢失已提交进度。
- [x] `cold-start` CLI 重复执行返回同一 ID，且命令不直接调用 Provider。

## 可能触点

- `advisor/market_daily/service.py`
- `advisor/market_daily/cli.py`
- `pyproject.toml`
- `tests/advisor/market_daily/test_service.py`
- `tests/advisor/market_daily/test_cli.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/market_daily/test_service.py \
  tests/advisor/market_daily/test_cli.py \
  tests/advisor/market_daily/test_service_recovery.py \
  tests/advisor/market_daily/test_engine_recovery.py
```
