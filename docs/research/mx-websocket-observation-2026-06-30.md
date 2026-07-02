# MX 网页端 WebSocket 观测记录

## 范围

- 目标：`https://mx.2026.naaifu.cn/`
- 观测时间：2026-06-30 01:45–01:50（Asia/Shanghai）
- 工具：Chrome DevTools MCP
- 方式：在已登录页面刷新前注入被动监听器，仅记录入站 WebSocket 帧

## 结论

页面使用 Socket.IO（Engine.IO v4）建立 WebSocket 长连接，不是普通 HTTP 长轮询。

```text
wss://mx.2026.naaifu.cn/business-api/5/socket.io/?EIO=4&transport=websocket
```

已确认的握手信息：

- 请求方法：`GET`
- 状态：`101 Switching Protocols`
- `Connection: upgrade`
- `Upgrade: websocket`
- 服务端：nginx
- Engine.IO 参数：`EIO=4`
- 传输方式：`transport=websocket`

## 协议行为

- Engine.IO 握手声明：
  - `pingInterval`: 25,000 ms
  - `pingTimeout`: 20,000 ms
  - `maxPayload`: 1,000,000 bytes
- Socket.IO 使用 `/msg` 命名空间。
- 入站 `2` 帧约每 25 秒出现一次，属于 Engine.IO 心跳。
- 观测到的业务帧模式：

```text
42/msg,<ack-id>["va",<timestamp-ms>]
```

在本次窗口内观测到 3 次 `va` 事件，间隔约 87–91 秒。`va` 的业务含义尚未确认；载荷仅包含毫秒时间戳。

## 连接稳定性

- 第一条观测连接以关闭码 `1006` 异常断开。
- 页面约 1.2 秒后自动重连。
- 第二条连接在观测结束时保持打开。
- 观测期间没有收到可识别的聊天、资讯正文或其他实际内容。

## 安全与数据处理

- 未发送任何业务消息。
- 未记录出站帧。
- 未保存登录令牌、Cookie、Socket.IO 会话 ID 或 WebSocket 调试标识。
- 文档中的动态会话字段均已省略。

## 监听器状态

观测结束后已对 MX 页面执行普通刷新，移除注入的监听器。随后验证：

- `window.__codexWsRecords` 不存在；
- `window.WebSocket` 已恢复为原生 `WebSocket` 构造器。

如需再次监听，必须重新注入监听器；关闭或刷新页面也会结束当前监听。
