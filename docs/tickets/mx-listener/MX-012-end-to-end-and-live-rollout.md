---
id: MX-012
status: in_progress
depends_on: [MX-003, MX-004, MX-007, MX-008, MX-011]
adrs: [0049, 0058, 0059, 0060, 0061, 0062, 0063, 0064, 0065, 0066, 0067, 0068]
---

# 完成离线端到端、运维文档与实机上线

## 结果

一套离线验收贯通 Listener 生命周期、RID 配置、历史资讯、RID-scoped Snapshot 和 Agent Access Publication；全部通过后，再无损迁移真实事件库并安装 MX KeepAlive LaunchAgent，验证人工授权恢复链路。

## 范围

- 建立纯离线端到端 fixture：fake CDP、临时事件/媒体库、临时 RID 配置、临时 Research Catalog、fake clock、fake LaunchAgents 和 fake Codex executor。
- 贯通状态迁移：无 Chrome → 无授权页面 → listening → CDP 断线 → 自动恢复 → SIGTERM；验证三维状态、租约、退避、sleep assertion 和零浏览器操作。
- 通过 Web API/UI 添加临时 RID、接收文本/图片事件、检索历史、移除 RID并继续浏览；验证 raw payload、source URL 和本地路径从未进入响应。
- 发布两个具有重叠/不同 RID Feed 的 Agent access revisions，显式修订 Team，构建共享 Snapshot；验证 Feed union 只物化一次且 Invocation 只能看到精确子集。
- 撤销一个 pinned RID 后只阻断依赖 Agent/Team；重新授权或发布移除 Feed 的 Agent/Team 新版本后恢复，其他 Team 全程继续。
- 更新 `agents.md`、`docs/mx-listener-operations-manual.md` 与 `docs/research-engine-operations.md`，以新 Service/WebUI 流程替换日常前台终端步骤，同时保留明确的手工恢复入口。
- 运维文档说明：安装/启动/停止/status、三维状态、RID 编辑、历史浏览、Agent Access 修订、日志、备份、授权恢复和安全边界。
- 离线全套通过后，对真实 `data/state/events.sqlite` 先做只读基线计数与 Schema 检查，再执行幂等状态/FTS 迁移；事件、媒体、任务、计数、失败和关联必须不变。
- 精确确认没有旧 Collector 进程或有效 lease 后，渲染、安装并加载 `com.ahunter.mx-listener`；不得删除数据库、媒体、真实 RID 或用户 Chrome profile。
- 实机验收顺序：
  1. Chrome 未准备时 Service 为 live + `waiting_for_chrome`，且不阻止 idle sleep；
  2. 用户从 WebUI 显式启动专用 Chrome、自行登录并打开 MX 页面后自动进入 `listening` 并持有 sleep assertion；
  3. 页面消失时自动进入等待并释放 assertion；
  4. 用户恢复页面后无需重启自动继续；
  5. WebUI、CLI 与数据库状态一致，Market Daily 不受影响。
- 对遗留 `collector-guardian.pid` 等标记只做精确识别和记录；除非用户单独授权，不删除或复用它们作为状态权威。
- 在本 ticket 追加真实验收记录、时间和有限计数，不记录 RID、消息正文、页面/调试 URL、PID 之外的敏感会话信息或投资结论。

## 不包含

- Listener 或恢复流程自动启动 Chrome、任何页面操作、自动登录、推断 RID、真实 Codex 投研结论、真实交易或删除历史 MX 数据。
- 未经用户授权清理旧 runtime 文件、Chrome profile、事件、媒体、Agent/Team Manifest 或报告。

## 验收条件

- [x] 离线 E2E 覆盖 Service 恢复、WebUI 配置/资讯、Feed union、Agent/Team 版本化和撤销阻断全链路。
- [x] 完整 Node、Python、frontend 测试和 build 在干净无代理 shell 中通过；自动化过程零实时网络和零真实状态改动。
- [x] 真实事件库迁移前后所有既有表计数、事件—媒体关联和内容哈希不变，FTS/控制面可幂等重建。
- [x] MX LaunchAgent 加载后只有一个实例持有 lease；stop/start 幂等且不影响 Market Daily、API、frontend 或 Chrome。
- [x] WebUI 的独立显式动作只能启动固定隔离 Chrome，不接收 URL，不操作页面，并只返回有限聚合状态。
- [ ] 用户提供浏览器前置条件时自动 listening，条件撤销时等待而不 crash-loop、不操作页面、不伪造登录状态。
- [ ] adaptive sleep、日志、WebUI 三维状态和 CLI 状态在实机一致；事件静默不产生错误告警。
- [x] RID WebUI 不改变历史；Agent Access 只能显式逐 RID 发布并且 Team 不自动升级。
- [x] 运维文档可从重启后的 Mac 引导用户恢复服务，且不暴露凭据、RID 内容、调试标识或危险命令。
- [x] `git diff --check` 通过，用户已有无关改动未被覆盖；真实验收记录已追加。

## 可能触点

- `tests/integration/mx-listener-service-end-to-end.test.mjs`
- `tests/integration/listener-control-plane-migration.test.mjs`
- `tests/advisor/research/test_mx_agent_access_end_to_end.py`
- `tests/advisor/test_mx_web_end_to_end.py`
- `src/events/migrate-listener-schema.mjs`
- `scripts/migrate-listener-schema.mjs`
- `src/services/mx-listener-service.mjs`
- `advisor/mx/chrome_launcher.py`
- `tests/unit/mx-listener-service.test.mjs`
- `frontend/src/**/*.test.tsx`
- `agents.md`
- `docs/mx-listener-operations-manual.md`
- `docs/research-engine-operations.md`
- `data/state/events.sqlite`
- `~/Library/LaunchAgents/com.ahunter.mx-listener.plist`
- `logs/mx-listener.out.log`
- `logs/mx-listener.err.log`
- 本 ticket 的“验收记录”小节

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs'
```

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor'
```

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node frontend/node_modules/vitest/vitest.mjs run --root frontend --reporter=dot'
```

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node frontend/node_modules/typescript/bin/tsc --noEmit -p frontend/tsconfig.json'
```

```bash
git diff --check
```

## 验收记录

### 离线验收记录（2026-08-08）

- 新增纯临时 fixture 覆盖 Listener 的 Chrome 缺失、授权缺失、被动监听、断线恢复与终止；fake CDP 只接收 `Network.enable`，不执行任何页面控制。
- 新增临时 RID、事件库与媒体 fixture 覆盖本地 Web API 的启停、RID 增删、历史浏览、受控媒体和字段脱敏。
- 新增临时 Catalog、RID 配置、事件库与不调用 Codex 的执行器覆盖重叠 Feed union、精确 Agent 可见性、显式 Team 修订、RID 撤销阻断和移除 Feed 后恢复。
- 新增显式 `--check`/`--apply` 两阶段 Listener Schema 工具；写入阶段持有单一事务、拒绝有效租约，并以不对外输出的逐行业务指纹验证事件、媒体、任务、计数、失败、关联和内容哈希完全不变。
- 对真实事件库完成只读基线：完整性正常，既有记录为 4 个 ingest run、538 个事件、164 个媒体、164 个媒体任务、0 个解码失败和 342 个采集计数，三类关联异常均为 0；控制面与 FTS 尚待真实迁移。
- 使用 SQLite 在线备份生成临时真实数据副本，连续两次执行迁移后上述计数和全部业务指纹均不变，控制面与 FTS 均就绪且无需再次迁移；临时副本随后精确删除，真实事件库没有写入。
- 完整 151 个 Node 测试、672 个 Python 测试、68 个前端交互测试、前端类型检查和生产构建均在无代理新 shell 中通过，`git diff --check` 无错误；没有连接真实 MX、操作 Chrome、修改真实 RID、事件库、媒体或 LaunchAgent。

### 实机验收记录（2026-08-08 12:54–13:11，进行中）

- 用户明确授权 MX-012 后，确认事件库为权限 `0600` 的常规 WAL 文件、`quick_check` 正常，没有旧 Collector/MX 进程、有效 Listener 租约或已加载的 MX LaunchAgent；遗留 guardian PID 文件只记录而未删除。
- 对真实事件库连续执行两次事务迁移并再次只读核验：4 个 ingest run、538 个事件、164 个媒体、164 个媒体任务、0 个解码失败和 342 个采集计数保持不变，三类关联异常均为 0；逐行业务指纹、事件—媒体关联和内容哈希不变，控制面与 538 行安全文本 FTS 均就绪。
- 渲染的用户级 plist 通过 `plutil` 校验并加载。首次实机启动暴露 LaunchAgent 在默认 `process.cwd()` 上停滞、尚未认领租约的问题；采样复现两次后，将默认根目录固定为从模块 URL 解析，并新增禁止 `process.cwd` 时仍能构造 Service 的回归测试。原始实机红信号随后转绿，没有遗留调试日志或临时文件。
- 连续 stop 两次和 start 两次均得到幂等结果；最终只有一个 Listener 进程和一个有效租约，旧实例正常退出，LaunchAgent 观察期内未 crash-loop，心跳持续续期。
- Chrome 尚未由用户准备时，数据库、CLI 和真实 Web API 一致报告 `live / waiting_for_chrome / healthy`；活动时间均为空，Listener 没有子 `caffeinate` 进程，未持有项目防休眠 assertion。
- Market Daily 在迁移、启停、修复和回归前后始终保持已加载、运行中和有效独立租约，尚未到 21:00 执行窗口；MX 操作没有改变其请求或 Run 状态。
- 修复后完整 152 个 Node 测试、672 个 Python 测试、68 个前端交互测试、前端类型检查和生产构建全部通过，`git diff --check` 无错误。

### WebUI 专用 Chrome 启动入口（2026-08-08，待用户点击）

- 新增独立 `DedicatedChromeLauncher.start()` 深模块与 `POST /api/mx/chrome/start`：只接受本地同源空 JSON 请求，只启动固定 Chrome、固定 loopback 端点和权限收紧的隔离 profile；不接收 URL，不导航、登录、操作或读取页面。
- WebUI 新增“启动专用 Chrome”按钮。启动器与 Listener 生命周期完全分离；重复请求会先识别已就绪实例，响应只包含 `changed`、`ready` 和有限中文提示，不包含端口、profile、PID 或调试标识。
- 自动化以临时可执行文件、临时 profile、fake endpoint 和 fake process launcher 覆盖已就绪、固定参数、占用冲突、symlink 拒绝和有界等待；测试没有启动真实 Chrome。
- 本机 A Hunter API 与 frontend 已用当前源码启动；只读 HTTP 核验确认 `/mx` 页面包含按钮、新 POST 路由存在，Listener 仍为 `live / waiting_for_chrome / healthy`。真实启动端点尚未由自动化调用，留给用户显式点击。
- 首次实机点击复现 400：Vite 代理保留页面 Origin 但把上游 Host 改为 API 端口，导致严格同源校验误拒绝。代理现已显式 `changeOrigin: false`，后端规则未放宽；同一真实代理 POST 使用 fake launcher 从 400 转为 200，且真实 Chrome 未被诊断过程启动。
- 最终回归为 152 个 Node 测试、679 个 Python 测试和 69 个前端交互测试全部通过；TypeScript 检查、生产构建和 `git diff --check` 通过。

仍待用户从 WebUI 点击“启动专用 Chrome”、自行登录并打开已授权 MX 页面后完成 `listening → 页面撤销 → 等待且释放 assertion → 页面恢复 → 自动 listening` 实机链路；服务在此之前保持健康等待，不自动操作页面或伪造授权状态。
