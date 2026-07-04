# Event foundation collector and legacy MX diagnostics

Phase 1 的唯一常规启动路径是 `scripts/run-collector.mjs`。它通过只读 Chrome DevTools `Network` 事件被动收集入站帧，不导航、刷新、注入脚本或操作页面。

## 文件

- `scripts/run-collector.mjs`：Phase 1 唯一常规收集器。
- `scripts/self-test.mjs`：离线测试和状态报告。
- `scripts/smoke-test.mjs`：用户已启用调试 Chrome 时的只读在线检查。
- `monitor-init.js`、`decode-room-msg.mjs`：默认禁用的旧版诊断工具；不属于收集器或 smoke test。

## Event foundation operations

Only the user may authorize RIDs in `config/allowed-rids.yaml`; an empty list is the safe, intentionally inactive default. Never infer or add a RID from observed traffic. Use Chrome DevTools only, never Computer Use, and stop to ask the user to log in if authorization expires. Do not store credentials or generate reports after a data-quality failure. Phase 1 is passive collection only and never performs real trading.

From the repository root, run the offline self-test before every start and after every code change:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs
```

Run the live smoke check only when the user has already enabled Chrome debugging:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/smoke-test.mjs
```

Before the first collector start, preserve legacy decoded output once:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/quarantine-legacy-output.mjs
```

Start the passive collector without navigating or reloading the page:

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/run-collector.mjs --cdp http://127.0.0.1:9222
```

Stop the collector with `Ctrl-C`. After a failure, keep it stopped, resolve configuration or have the user restore the authorized login, and rerun the offline self-test before restarting.

Operational behavior: expired raw payloads are purged at startup and every 24 hours. Persisted media jobs are fetched in bounded batches until no due jobs remain, retry independently after restart with bounded exponential backoff, and commit media metadata plus job completion in one transaction. Completed jobs are never downloaded again. SIGINT/SIGTERM handling is active before startup maintenance or media work: shutdown stops frame intake and immediately starts every already-queued frame, temporarily exceeding the normal runtime concurrency cap while queued plus active work drain through a 30-second grace period. It then aborts frame/media/download work through one shared signal and waits for settlement before closing SQLite once. The live smoke check bounds target discovery, WebSocket opening, and `Network.enable` to 5 seconds each, unregisters its listener, and closes its CDP client on every path. An available CDP target list without the exact MX page exits terminally as `authorization_required`; transport failures still retry. The RID allowlist supports atomic live reload, and an invalid or unreadable replacement fails closed to an empty set.

## Legacy diagnostic tooling (disabled by default)

The page-injection workflow below is legacy diagnostic tooling, not a Phase 1 operating path. It is disabled by default, is never used by `scripts/run-collector.mjs` or `scripts/smoke-test.mjs`, and may be used only when the user explicitly requests that specific diagnostic action. Do not use it for routine startup, reconnect, login recovery, collector recovery, or smoke testing. It must not be used as a workaround for a failed collector or expired authorization.

Even when explicitly requested, use Chrome DevTools only, do not use Computer Use, do not record outgoing frames, and do not send Socket.IO business events. Stop and ask the user to restore authorization if login has expired.

### 旧版诊断依赖

```bash
npm --prefix scripts/mx-websocket install
```

### 用户明确请求时的旧版页面注入诊断

Only after the user explicitly requests this legacy diagnostic, the complete `monitor-init.js` may be supplied as the Chrome DevTools MCP `navigate_page` `initScript` during one page reload. The injected listener records inbound frames only and sends no Socket.IO events. Its state and cache can be inspected in the page context:

```js
window.__mxWsMonitor.isActive();
window.__mxWsMonitor.snapshot();
window.__mxWsMonitor.roomMessageFrames();
```

停止监听并恢复原生 `WebSocket`：

```js
window.__mxWsMonitor.stop();
```

页面刷新或关闭也会清除监听器。

### 导出旧版诊断帧

导出结果应保存为 JSON 数组。数组元素可以是：

- 完整 Socket.IO 帧字符串；
- 仅包含加密载荷的字符串；
- 带有 `data` 或 `payload` 字符串字段的对象。

完整帧示意：

```text
42/msg,["room_msg","<encrypted-payload>"]
```

请勿把 Cookie、登录令牌或 Socket.IO 会话 ID 写入导出文件。

### 离线解码旧版诊断帧

```bash
npm --prefix scripts/mx-websocket run decode -- \
  --input raw-frames.json \
  --date 2026-07-02
```

`--date` 必须与消息到达时浏览器所在时区的日期一致；省略时使用本机当天日期。

成功解码的内容会按消息 ID 去重并持续累积到固定文件：

```text
a_hunter/data/mx-websocket/decoded-room-messages.json
```

解码失败的帧不会写入固定文件。需要临时改用其他输出位置时，可以传入 `--output <path>`。

输出会包含：

- 解码后的消息对象；
- 递归解析后的 `msg` 内容；
- 独立的文本片段列表；
- 从 `type: "pic"`、`type: "image"`、`type: "img"` 和明文 URL 中提取的图片地址。

### 已确认的旧版解码链

1. `LZString.decompress(payload)`；
2. 对 `YYYY-MM-DD` 计算 MD5；
3. AES key：MD5 十六进制字符串前 16 个字符（按 UTF-8 字节使用）；
4. AES IV：MD5 十六进制字符串第 9–14 个字符（按 UTF-8 字节使用）；
5. AES-CBC + PKCS7 解密；
6. UTF-8 解码并执行 `JSON.parse`。

仅应用于你有权访问和分析的数据。
