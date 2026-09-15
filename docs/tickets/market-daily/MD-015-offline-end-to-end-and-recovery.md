---
id: MD-015
status: complete
depends_on: [MD-012, MD-013, MD-014]
adrs: [0029, 0033, 0037, 0038, 0045, 0048, 0049, 0050, 0051]
---

# 完成离线端到端与恢复验收

## 结果

使用小型固定沪深股票池完整验证 Request → 常驻 Service → 股票池 → 主备来源 → 冷启动 → 21:00 补洞 → Research Snapshot → WebUI 状态，并证明每个持久化边界都可安全恢复。

## 范围

- 建立离线端到端 fixture：正常股票、ST、新上市、窗口内退市、停牌、主源失败备源成功、两源失败和数据冲突。
- 从空 SQLite 启动 Service，提交冷启动 Request，推进到 `partial`，修复来源后恢复并封印 `complete`。
- 用 fake clock 跨过 21:00，验证正常追加一天、连续漏跑补洞和休市 no-op。
- 构建一个单股 Research Snapshot，验证 raw 价格、Research Price Series、因子哈希和 scope-aware readiness。
- 验证 WebUI/API 对服务健康、进度和失败项的只读呈现。
- 模拟 Service 在认领、Provider 返回、逐股提交、Run 封印和 SIGTERM 时中断。
- 更新中文操作文档：安装、提交冷启动、查看进度、日志、partial 排查、停止和恢复。
- 运行完整 Python、前端和 Node 离线检查。

## 不包含

- 真实网络请求、清空实际数据库、安装实际 LaunchAgent 或运行 MX Listener。

## 验收条件

- [x] 成功路径最终只有一个 complete 冷启动 Run，且无重复 K 线。
- [x] partial 路径保留已成功数据，恢复后只重试缺口。
- [x] 冲突不覆盖旧值，停牌不生成假 K 线，两源失败不被误判为休市。
- [x] 21:00 触发、重启恢复和双实例租约都具有确定性测试。
- [x] Research Snapshot 与 WebUI 同一时刻读取到一致状态。
- [x] 全量 Python、前端测试、Node self-test 和 `git diff --check` 通过。

## 可能触点

- `tests/advisor/market_daily/test_end_to_end.py`
- `tests/advisor/market_daily/test_service_recovery.py`
- `tests/advisor/research/test_end_to_end.py`
- `docs/market-daily-operations.md`
- `docs/research-engine-operations.md`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor
(
  cd frontend
  /Users/mac/.local/share/chrome-devtools-mcp/node/bin/node node_modules/typescript/bin/tsc --noEmit -p tsconfig.json
  /Users/mac/.local/share/chrome-devtools-mcp/node/bin/node node_modules/vitest/vitest.mjs run --reporter=dot
)
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
git diff --check
```
