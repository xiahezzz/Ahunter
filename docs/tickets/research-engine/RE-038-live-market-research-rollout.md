---
id: RE-038
status: pending_user_authorization
depends_on: [RE-037]
adrs: [0006, 0017, 0050, 0073, 0078, 0079, 0082, 0085, 0095]
---

# 完成实机上线与首份 Market Team 报告

> 当前状态：离线实现、验证命令和上线保护已就绪；实际安装 Service、调用真实 Provider 或提交首份 Market Request 必须等待用户的明确授权。

## 结果

在离线验收全部通过后，以可恢复、非破坏方式启用 Research Service，并通过本机 WebUI 生成和核验首份真实 `a_share_market_overview@1` 报告。

## 范围

- 先运行完整离线验证、Catalog/DB migration dry run、runtime environment 校验与 `advisor-research preflight`；任何失败都停止上线，不生成研究结论。
- 备份/记录现有 Advisor DB schema version、Service Set status 和 reports/artifact indexes，不清空 Market Daily、MX Events、Research history 或用户配置。
- 仅在用户明确批准时渲染、安装或加载 Research Service LaunchAgent；使用 `.venv-runtime`，不读取或复制 Codex session material。
- 启动后验证 Service lease/heartbeat、idle 状态和 WebUI offline→online 转换；不自动提交研究。
- 由用户在 WebUI 显式提交一次 `a_share_market_overview@1` Market Request，记录 Request ID，并观察 queued→snapshot→agents→decision→publishing→terminal 的真实状态。
- 核验 Whole-Market Snapshot provider/provenance、coverage、taxonomy version/age、Market Information source tiers、三名 Agent methods 和 Report exact versions。
- passed 或 partial 报告必须能在 Report Explorer 按 publication time 找到并安全打开；若 blocked/failed，保留记录、诊断有限原因并修复后以 rerun 新建 Request，绝不覆盖原记录。
- 验证同日第二次显式提交不受每日限制，取消一条测试 queued Request，并确认历史与全局队列真实反映。
- 在 ticket 内记录实机验收时间、版本、Request IDs、有限状态与验证命令，不粘贴研究正文、Provider body、prompt、日志或任何凭据。

## 不包含

- 自动安装 LaunchAgent、自动运行市场研究、Market Daily 重置、MX Chrome 操作、真实交易或依据验收报告下单。
- 为追求 passed 而降低覆盖/新鲜度、切换未批准来源、重写失败记录或生成替代结论。

## 验收条件

- [ ] 离线 suite 与 preflight 通过后才允许启动；运行时明确使用 `.venv-runtime`。
- [ ] Research Service 单实例在线，WebUI 状态真实，API/浏览器重启不丢 Request 或进度。
- [ ] 首份 Market Report 固定 Team/Agent/Pipeline/Product/Policy 版本，且不包含个股推荐、stance、价格或仓位。
- [ ] coverage/taxonomy/information 任一质量不足时按 Insight 产生 partial/blocked，而不是伪造 passed。
- [ ] Report Explorer 能分页定位并打开报告；同日多次提交、queued cancel 和 rerun 行为符合控制面记录。
- [ ] 上线操作不删除或改写任何既有 Research、Market Daily、MX 或 Team artifacts，并留下可审计验收记录。

## 可能触点

- `docs/tickets/research-engine/RE-038-live-market-research-rollout.md`
- `docs/research-engine-operations.md`
- `advisor/runtime_env.py`
- `advisor/scheduler/launchd.py`
- `config/research/`
- `data/`
- `reports/`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs && ./.venv-runtime/bin/advisor-research preflight && ./.venv-runtime/bin/advisor-services status'
```
