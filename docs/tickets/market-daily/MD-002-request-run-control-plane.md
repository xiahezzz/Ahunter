---
id: MD-002
status: complete
depends_on: [MD-001]
adrs: [0033, 0038, 0045, 0050]
---

# 建立 Request、Run 与进度控制面

## 结果

CLI 可以幂等提交持久化 Market Daily Request，常驻 Service 可以独占认领请求，并通过明确的 Run 与逐证券状态记录进度、恢复点和失败原因。

## 范围

- 定义 Request、Ingestion Run、Run Item、Service Lease 的合法状态和转换。
- 在 `advisor.sqlite` 建立请求、运行、逐证券任务和服务租约表；所有控制面写入使用短事务。
- 冷启动请求按目标交易日和五年窗口幂等；重复提交返回原 request/run ID。
- Run 保存固定的目标日期、窗口、股票池哈希、总数、完成数、失败数、更新时间和最终状态。
- Run Item 保存代码、请求区间、尝试次数、选中来源、最后错误和恢复游标，不复制完整行情数据。
- 实现独占认领、心跳、租约过期接管和进程崩溃后的 `running → pending` 恢复规则。
- 提供稳定的状态查询模型，供 CLI、API 和 WebUI 共用。

## 不包含

- Provider 调用、实际 K 线写入、21:00 计时器或 LaunchAgent。
- WebUI 页面或可变 API。

## 验收条件

- [x] 状态转换拒绝跳阶段、终态回退和两个 Service 同时认领。
- [x] 相同冷启动请求并发提交只产生一条有效请求和一个 Run。
- [x] 心跳失效后只有一个新实例能接管未完成工作。
- [x] 进度计数由 Run Item 确定性计算，不依赖进程内计数器。
- [x] 错误文本有长度上限且不保存响应正文或调试标识。
- [x] 崩溃恢复测试覆盖认领前、认领后、逐股提交后和完成封印前四个边界。

## 可能触点

- `advisor/market_daily/control.py`
- `advisor/market_daily/repository.py`
- `advisor/db/schema.sql`
- `tests/advisor/market_daily/test_control_plane.py`
- `tests/advisor/market_daily/test_service_recovery.py`
- `tests/advisor/market_daily/test_engine_recovery.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/market_daily/test_control_plane.py \
  tests/advisor/market_daily/test_service_recovery.py \
  tests/advisor/market_daily/test_engine_recovery.py
```
