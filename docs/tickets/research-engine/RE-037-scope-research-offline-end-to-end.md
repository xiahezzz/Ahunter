---
id: RE-037
status: done
depends_on: [RE-027, RE-028, RE-029, RE-030, RE-031, RE-033, RE-035, RE-036]
adrs: [0070, 0072, 0073, 0074, 0075, 0080, 0089, 0095, 0097, 0098, 0099]
---

# 完成 Scope Research 离线端到端与恢复验收

## 结果

一套完全离线的端到端测试证明 Market 与 Security research 共用同一持久化执行系统，并贯通 Scope 配置、数据快照、Agent/Pipeline、队列、WebUI 状态、报告分页和恢复。

## 范围

- 使用临时 Catalog、SQLite、Artifact Store、固定 Provider fixtures、fake clock 和 fake Codex 完成 Market 与 Security 两条完整路径。
- 验证新 Manifest 显式 Scope、legacy Manifest 默认 Security、同稳定身份 Scope 不变、Team 同 Scope 发布和任意历史版本启动。
- 同日提交多个相同 Team/Subject Requests，验证无每日限制、单 Cycle 全局执行、manual FIFO/priority、Service offline pending 和恢复后顺序。
- 覆盖 Manual Security accepted-time Boundary 与 Manual Market execution/snapshot Boundary，并证明 snapshot 后证据不可见。
- Market happy path 产生三个 typed Insights 与 passed report；taxonomy stale、information missing、Agent timeout/schema-invalid 分别产生正确 partial；无可用 Insight 产生 blocked 无 report。
- 在 queued/running 各取消一次，并从 terminal Record rerun；验证 artifacts/history 不被删除或覆盖。
- 幂等导入既有 legacy Cycles，再生成 150+ mixed Records，贯通 Team 跨版本筛选、exact version、status filter、publication-time pagination 和安全详情渲染。
- 模拟 API、WebUI、Research Service 分别重启，验证 durable truth 不依赖浏览器/进程内存。
- 更新 `docs/research-engine-operations.md`，说明服务状态、手动启动、Scope 输入、取消、rerun、报告查询、partial 和故障恢复。

## 不包含

- 真实网络、真实 Codex session、真实 LaunchAgent 安装、真实报告结论、Market Daily reset 或实际交易。
- 多用户、远程访问、Team 比较、推荐自动执行或测试中等待真实交易时段。

## 验收条件

- [ ] 一条离线 E2E 从 Web API submit 贯通 Service、Snapshot、Agents、Market Pipeline、publication、Report Explorer detail。
- [ ] Security 既有路径行为和报告 schema 全部回归；Market 新路径不依赖 sentinel code 或第二套 executor。
- [ ] crash/restart、cancel/rerun、partial/blocked/failed、legacy import 和 150+ 分页均有稳定测试。
- [ ] 所有 Provider tests 使用 fixtures；测试网络被 clean-shell proxy 环境隔离。
- [ ] 完整 Python tests、前端 tests/build、repository Node self-test 和 `git diff --check` 通过。
- [ ] 运维文档不包含凭据、Codex session、Provider response body、内部调试 URL 或真实用户数据。

## 可能触点

- `tests/advisor/research/test_scope_research_end_to_end.py`
- `tests/advisor/research/test_service_recovery.py`
- `tests/advisor/test_research_web.py`
- `frontend/src/App.test.tsx`
- `docs/research-engine-operations.md`
- `scripts/self-test.mjs`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor && cd frontend && npm test -- --run && npm run build && cd .. && /Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs && git diff --check'
```
