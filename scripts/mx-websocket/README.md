# MX WebSocket tools

用于在已授权、已登录的 MX 网页端会话中被动记录入站 WebSocket 帧，并离线解码 `room_msg`。

## 文件

- `monitor-init.js`：页面刷新前注入的只读 WebSocket 监听器。
- `decode-room-msg.mjs`：解码原始 `room_msg` 帧或其加密载荷。
- `package.json`：解码依赖与运行命令。

监听器不会记录出站帧，也不会主动发送 Socket.IO 事件。

## 安装依赖

```bash
npm install
```

## 开启监听

将 `monitor-init.js` 的完整内容作为 Chrome DevTools MCP `navigate_page` 的 `initScript`，并执行一次页面刷新。监听状态和缓存可在页面上下文中读取：

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

## 导出原始帧

导出结果应保存为 JSON 数组。数组元素可以是：

- 完整 Socket.IO 帧字符串；
- 仅包含加密载荷的字符串；
- 带有 `data` 或 `payload` 字符串字段的对象。

完整帧示意：

```text
42/msg,["room_msg","<encrypted-payload>"]
```

请勿把 Cookie、登录令牌或 Socket.IO 会话 ID 写入导出文件。

## 解码

```bash
npm run decode -- \
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

## 已确认的解码链

1. `LZString.decompress(payload)`；
2. 对 `YYYY-MM-DD` 计算 MD5；
3. AES key：MD5 十六进制字符串前 16 个字符（按 UTF-8 字节使用）；
4. AES IV：MD5 十六进制字符串第 9–14 个字符（按 UTF-8 字节使用）；
5. AES-CBC + PKCS7 解密；
6. UTF-8 解码并执行 `JSON.parse`。

仅应用于你有权访问和分析的数据。
