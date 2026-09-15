---
id: RE-026
status: done
depends_on: [RE-012, RE-025]
adrs: [0072, 0073, 0074, 0080, 0090, 0091]
---

# 实现常驻 Research Service 与全局队列

## 结果

一个可恢复的 Research Service 独占认领 SQLite Research Requests，全系统同一时间最多运行一个 Research Cycle，并持续写入用户可读进度。

## 范围

- 建立 `ResearchService` 深接口，唯一拥有 Request claim、Research Cycle 调用、进度投影、取消协调、恢复和 Service Lease。
- 全局只运行一个 Research Cycle；Cycle 内 Agent Invocation 继续使用 Codex Execution Policy 的有界并发。
- active Cycle 永不抢占；在 Cycle 边界，queued manual Requests 按 accepted time FIFO 优先于尚未开始的 scheduled work，scheduled work 后续继续而不是丢弃。
- Manual Security Request 在后端接受时固定 Boundary；Manual Market Request 先记录 requested/accepted time，在 Service 真正开始并封存一次 Whole-Market Intraday Snapshot 时固定 Boundary。
- 将状态稳定投影为 queued、preflight、snapshot、agents x/y、当前 Decision Stage、publishing 与终态，并写 last-updated 和有限 reason code。
- queued 取消立即终止；running 取消设置共享 cancel signal，在安全阶段停止，必要时有界终止当前 Codex Invocation，并保留已提交 artifacts。
- 崩溃或重启只从持久化幂等阶段恢复，绝不尝试恢复失联 Codex 进程；租约阻止两个 Service 同时执行。
- Service 离线时 Request 保持 queued，控制面不伪造 running、失败或预计完成时间。

## 不包含

- LaunchAgent、Service Set CLI、Web API、WebUI、Market Data Provider 或新的 Decision Pipeline。
- 多 Cycle 并行、mid-Cycle 手动插队、按日期限次或删除取消记录。

## 验收条件

- [ ] 两个 Service 实例只有一个能持有租约并认领；同一 Request 不会产生两个 Cycle。
- [ ] manual FIFO、manual 对未开始 scheduled 的优先级、active Cycle 不抢占均由 fake clock/fixture 证明。
- [ ] Market 与 Security manual Boundary 分别在正确时刻固定并同时保留 requested time。
- [ ] 每个阶段重启都能从最近持久化边界恢复，不重复已完成 Agent Invocation 或发布。
- [ ] queued/running 取消均进入 `cancelled`，不会删除 artifacts；终态取消明确拒绝。
- [ ] Service 停止期间提交多条 Request 后仍全部保持 queued，恢复后按队列规则执行且无每日限制。

## 可能触点

- `advisor/research/service.py`
- `advisor/research/state_machine.py`
- `advisor/research/repository.py`
- `advisor/research/codex/executor.py`
- `tests/advisor/research/test_service.py`
- `tests/advisor/research/test_service_recovery.py`
- `tests/advisor/research/test_cycle_recovery.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/test_service.py tests/advisor/research/test_service_recovery.py tests/advisor/research/test_cycle_recovery.py'
```
