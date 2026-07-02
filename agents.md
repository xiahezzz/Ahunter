# MX WebSocket 监听与解码

当任务涉及 `https://mx.2026.naaifu.cn/` 的实时消息时，使用本目录下的工具：

- 监听器：`scripts/mx-websocket/monitor-init.js`
- 解码器：`scripts/mx-websocket/decode-room-msg.mjs`
- 详细说明：`scripts/mx-websocket/README.md`
- 固定输出：`data/mx-websocket/decoded-room-messages.json`

## 操作约束

- 使用 `chrome-devtools` MCP 连接用户已经登录的 Chrome。
- 不使用 Computer Use，除非用户另行明确要求。
- 监听器只记录入站帧，不发送 Socket.IO 业务事件。
- 不保存 Cookie、登录令牌、Socket.IO 会话 ID 或 Chrome 调试标识。
- 仅分析用户有权访问的数据。

## 开启监听

1. 使用 `mcp__chrome_devtools__list_pages` 找到 MX 网页端。
2. 使用 `mcp__chrome_devtools__select_page` 选择该标签页，默认不要置前。
3. 完整读取 `scripts/mx-websocket/monitor-init.js`。
4. 调用 `mcp__chrome_devtools__navigate_page`：
   - `type`: `reload`
   - `initScript`: 监听脚本的完整内容
5. 使用 `mcp__chrome_devtools__evaluate_script` 验证：

```js
() => ({
  active: window.__mxWsMonitor?.isActive?.() === true,
  connections: window.__mxWsMonitor?.snapshot?.().length || 0,
  roomMessages: window.__mxWsMonitor?.roomMessageFrames?.().length || 0,
})
```

页面刷新、关闭或重新导航后监听器会消失。继续监听前必须重新检查，不能假设它仍然存在。

## 导出消息帧

调用 `mcp__chrome_devtools__evaluate_script`，把结果保存为 JSON 文件，例如 `/private/tmp/mx-room-msg-current.json`：

```js
() => window.__mxWsMonitor?.roomMessageFrames?.() || []
```

导出项保留 `at` 和 `data` 字段。`at` 用于跨午夜时按每条消息的实际日期派生解密密钥。

## 解码并写入固定文件

依赖目录为 `scripts/mx-websocket/node_modules`。如果依赖缺失，在工具目录执行：

```bash
PATH=/Users/mac/.local/share/chrome-devtools-mcp/node/bin:$PATH \
  /Users/mac/.local/share/chrome-devtools-mcp/node/bin/npm install
```

解码带 `at` 时间戳的导出文件：

```bash
cd /Users/mac/Documents/Ahunter/a_hunter/scripts/mx-websocket
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node \
  decode-room-msg.mjs \
  --input /private/tmp/mx-room-msg-current.json
```

如果输入只是字符串数组、没有 `at` 字段，必须明确提供消息到达日期：

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node \
  decode-room-msg.mjs \
  --input /private/tmp/mx-room-msg-current.json \
  --date YYYY-MM-DD
```

成功解码的消息固定写入：

```text
/Users/mac/Documents/Ahunter/a_hunter/data/mx-websocket/decoded-room-messages.json
```

行为约定：

- 仅成功解码的消息进入固定文件。
- 按消息 `id` 去重；没有 `id` 时依次使用 `oid` 或内容组合键。
- 多次执行会累积新消息，不会重复追加同一消息。
- 图片地址写入每条消息的 `content.imageUrls`。
- 文本片段写入每条消息的 `content.texts`。
- `msg` 内嵌的 JSON 数组会被递归解析，包括 `type: "pic"`、`image` 和 `img`。
- 解码失败应报告数量与原因，但不得把失败帧混入固定文件。

## 已确认的解码流程

1. `LZString.decompress(payload)`；
2. 对消息日期 `YYYY-MM-DD` 计算 MD5；
3. AES key 使用 MD5 十六进制字符串前 16 个字符的 UTF-8 字节；
4. AES IV 使用 MD5 十六进制字符串第 9–14 个字符的 UTF-8 字节；
5. 使用 AES-CBC + PKCS7 解密；
6. UTF-8 解码并执行 `JSON.parse`；
7. 递归解析 `msg` 字段，分别提取文本和图片 URL。

## 停止监听

使用 `mcp__chrome_devtools__evaluate_script`：

```js
() => ({ stopped: window.__mxWsMonitor?.stop?.() === true })
```

随后再次验证 `window.__mxWsMonitor?.isActive?.() !== true`。普通刷新也会移除监听器，但优先调用 `stop()`，以便立即恢复原生 `WebSocket`。
