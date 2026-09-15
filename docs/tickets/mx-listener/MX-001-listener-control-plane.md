---
id: MX-001
status: complete
depends_on: []
adrs: [0059, 0060, 0061]
---

# 建立 Listener 控制面、租约与三维状态契约

## 结果

MX 事件库拥有一个可原子认领、可恢复的单实例控制面，并以稳定契约分别表达 Listener Liveness、Readiness、Health 和只读活动时间。

## 范围

- 在 `data/state/events.sqlite` 的既有 Schema 上做幂等增量迁移，不复制或重写 Accepted MX Events、media、media_jobs、counters 或 decode_failures。
- 建立单例 Service Lease：实例 ID、启动时间、心跳时间和到期时间；只有租约持有者能更新运行状态，过期后新实例才能接管。
- 建立稳定状态快照：
  - Liveness 由有效租约和新鲜心跳证明；
  - Readiness 只允许 `starting`、`waiting_for_chrome`、`waiting_for_authorization`、`connecting`、`listening`、`stopping`；
  - Health 只允许 `healthy`、`degraded`、`failed`；
  - 活动字段包含 `connected_at`、`last_frame_at`、`last_accepted_event_at`，静默不改变 Readiness 或 Health。
- 记录有限、枚举化的 reason code，不写异常正文、CDP URL、页面 URL、RID、载荷或媒体 URL。
- 提供一个深 `ListenerControlPlane` 模块；调用者只学习 claim/transition/heartbeat/release/read snapshot，不直接拼 SQL 或自行判断租约。
- 所有时间为 Unix 毫秒或显式 `Asia/Shanghai` 输出；拒绝倒退心跳、未来时间、未知状态和非持有者更新。
- 进程正常退出保留最后活动时间但释放租约；异常退出依靠 TTL 变为离线，不能由陈旧状态伪装运行。

## 不包含

- CDP 重连循环、媒体下载、LaunchAgent、Web API、WebUI 或 Agent Data Product。
- 对真实事件库执行迁移；实机迁移由 MX-012 完成。

## 验收条件

- [x] 两个并发实例只能有一个 claim 成功；另一个得到稳定的 `already_running` 结果。
- [x] 同一实例可幂等续期，非持有者不能 transition、heartbeat 或 release。
- [x] 租约未过期时不能接管，过期后一项原子操作完成接管且不会出现双持有者。
- [x] Liveness、Readiness、Health 与活动时间能独立变化；无事件静默不会生成故障。
- [x] 旧数据库迁移后事件、媒体、任务、计数与失败记录的数量和关联完全不变。
- [x] 状态行和测试错误中不含 RID、页面地址、调试地址或载荷片段。

## 可能触点

- `src/events/schema.sql`
- `src/events/event-store.mjs`
- `src/services/listener-control-plane.mjs`
- `tests/unit/listener-control-plane.test.mjs`
- `tests/integration/listener-control-plane-migration.test.mjs`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node --test tests/unit/listener-control-plane.test.mjs tests/integration/listener-control-plane-migration.test.mjs'
```
