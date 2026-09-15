# MX Listener 运维手册

MX Listener 是本机常驻的被动资讯监听服务。它只通过 Chrome DevTools 的网络事件读取用户已经打开、登录并授权的 MX 页面；Listener 本身不会打开 Chrome、导航页面、刷新、点击、输入、注入代码或请求凭据。WebUI 另有一个由用户显式点击的“启动专用 Chrome”动作，也可按用户请求执行 `ahunter mx chrome start` 调用同一接口，它只打开固定的隔离 Chrome，不打开任何网址，也不参与 Listener 自动恢复。日常操作使用本地 WebUI 或 `advisor-services`，不再以前台 `run-collector` 进程作为日常入口。

## 安全边界

- 只有用户可以提供和授权 RID；不得从流量、历史记录或页面内容推断、添加 RID。
- `allowed_rids: []` 是有效的停用状态：不会接收新内容或下载新图片，但不会删除既有历史。
- 不记录或展示凭据、Cookie、会话令牌、原始 Socket 帧、浏览器调试标识、来源 URL 或本地媒体路径。
- 不要用 `sudo` 运行服务、测试、备份或浏览器。不要手工删除事件库、媒体、旧 PID 文件或 LaunchAgent。
- 只有用户显式启动专用浏览器、自行登录并打开 MX 页面后，服务才可能进入监听状态；等待不是故障，也不需要页面自动化来“修复”。

项目根目录为 `/Users/mac/Documents/Ahunter/a_hunter`，离线检查使用 `/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node`。以下命令均从项目根目录执行。

## 日常查看、启动和停止

本地 WebUI 的“MX 监听”页面和 CLI 使用同一服务集。读取状态不会启动服务、接触浏览器、改 RID 或改历史。

```bash
./.venv-runtime/bin/advisor-services status
```

需要浏览器且用户已请求启动时，在 WebUI 点击“启动专用 Chrome”或显式执行 `rtk ahunter mx chrome start`。该按钮使用项目固定的 Chrome、loopback 调试配置和隔离 profile；重复点击时若专用实例已就绪则不会再启动。它不接收网址，不会替用户登录或打开 MX 页面。Chrome 窗口出现后，由用户自行登录并打开已授权的 MX 页面。

若本机已完成服务安装，可在 WebUI 点击“启动 MX Listener”或执行：

```bash
./.venv-runtime/bin/advisor-services start mx-listener
```

停止可在 WebUI 点击“停止 MX Listener”或执行：

```bash
./.venv-runtime/bin/advisor-services stop mx-listener
```

`start`/`stop` 对同一状态幂等。停止会卸载 Listener 并等待其租约释放；不会关闭 Chrome、修改 RID 或删除资讯。Market Daily 是独立服务，不受这些命令影响。

### 三维状态

- 存活性：`live` 表示当前实例持有有效租约；`offline` 表示未观察到有效租约。
- 就绪性：`waiting_for_chrome` 等待用户自行准备 Chrome，`waiting_for_authorization` 等待用户自行登录并打开 MX 页面，`connecting` 正在被动重连，`listening` 正在被动监听，`stopping` 正在停止。
- 健康度：`healthy` 正常；`degraded` 表示可恢复的配置、维护或睡眠抑制问题；`failed` 表示本地状态不可安全使用，需要排查。

只有进入 `listening` 时服务才持有防休眠 assertion；离开该状态会释放它。没有新消息本身不会被标为故障。

## 首次安装与首次启动

首次安装会渲染并写入用户的 LaunchAgent，因此必须先得到用户明确许可。安装前先创建运行环境并运行离线自检：

```bash
/opt/local/bin/python3.11 -m advisor.runtime_env
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/self-test.mjs'
```

首次上线还需要迁移 Listener 控制面与安全文本 FTS。先运行只读核验；它只输出业务表计数、关联异常计数和有限 Schema 状态，不输出 RID、正文、URL、路径或行指纹：

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node \
  scripts/migrate-listener-schema.mjs \
  --check \
  --database data/state/events.sqlite
```

只有在用户已明确许可真实迁移、Listener 没有有效租约，并且以下旧 Collector 进程检查没有结果时，才能执行写入：

```bash
pgrep -fl '[r]un-collector.mjs|[r]un-mx-listener-service.mjs'
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node \
  scripts/migrate-listener-schema.mjs \
  --apply \
  --database data/state/events.sqlite
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node \
  scripts/migrate-listener-schema.mjs \
  --check \
  --database data/state/events.sqlite
```

`--apply` 在一个写事务内重新采集基线并验证全部既有业务行的私有指纹；任何业务表计数、内容、事件—媒体关联或内容哈希发生变化都会回滚。它也会拒绝有效 Listener 租约。完成后的只读核验必须显示完整性正常、关联异常均为零、控制面与搜索就绪且无需迁移。遗留 `collector-guardian.pid` 仅作记录，不删除，也不作为进程或租约权威。

仅在首次真实启动前，且确认要保留遗留解码输出时，运行一次：

```bash
/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node scripts/quarantine-legacy-output.mjs
```

得到许可后，安装并加载：

```bash
./.venv-runtime/bin/advisor-services install mx-listener
./.venv-runtime/bin/advisor-services load mx-listener
./.venv-runtime/bin/advisor-services status
```

安装或加载并不启动、登录或操作 Chrome。用户可在 WebUI 显式点击“启动专用 Chrome”或请求执行 `ahunter mx chrome start`，然后自行登录并停留在已授权的 MX 页面；随后从 WebUI 或 `advisor-services start mx-listener` 请求启动 Listener。两个动作彼此独立，先后顺序不影响最终自动连接。不要把浏览器调试信息复制到终端记录、工单或聊天中。

## RID 编辑与历史资讯

在 WebUI 的“MX 监听”页面编辑 RID：输入用户明确授权的正整数、添加或移除后点击保存。页面以当前配置版本提交；若出现冲突，刷新并人工比较后再提交。`advisor-services status` 只显示数量；统一 CLI 的 `ahunter mx rids get` 返回已授权 RID 和版本，`ahunter mx rids replace --version VERSION --rid RID` 使用该版本替换完整列表（多个 RID 重复传入 `--rid`）。发生版本冲突时先重新读取、比较，不得自动覆盖。

RID 的保存是原子替换，Listener 会在下一次配置轮询时热加载。移除 RID 只改变今后的接收授权；历史事件仍可在“MX 资讯”中按“当前授权”或“已撤销”筛选、检索、分页和查看。页面只显示规范化文本和受控图片路由，不会把原始 payload、来源地址或存储路径发送到浏览器。

## Research 的 Agent Data Access

在 WebUI 的“研究配置”页面按以下顺序操作：

1. 在“Data Product”查看已发布产品及其历史；这里没有 Provider 连接参数，也不会启动研究。
2. 在“Agent Data Access”选择一个固定 Agent 版本，逐项选择 Data Product。对 `mx_events@2` 必须逐 RID 选择，不能选择“全部 RID”。
3. 点击发布会创建新的不可变 `agent@版本`，仅改变该 Agent 的数据访问；不会改 Team，也不会启用每日研究。
4. 如需让 Team 使用新 Agent，转到“研究团队”显式基于该 Team 创建新版本，再单独决定是否启用该版本的每日研究。

若某个已固定的 RID 被撤销，只有仍依赖它的 Agent/Team 会被阻断；其他 Team 继续可用。恢复方式是用户重新授权该 RID，或发布去掉该 RID 的新 Agent 版本并显式发布新的 Team 版本。历史 Agent 与 Team 不会被重写。

## 历史免责声明迁移核验

仅当用户已明确安排旧版内容迁移、并且 Listener 已停止时，才需要做这项只读核验。它只统计精确可移除位置的免责声明，不读取或输出消息正文；两个结果都必须为 `0` 才能认为迁移完成。

```sql
SELECT
  coalesce((
    SELECT count(*)
    FROM events AS e
    JOIN json_tree(e.parsed_content_json, '$.parsed') AS node
    WHERE node.type = 'text'
      AND trim(node.atom, char(9) || char(10) || char(11) || char(12) || char(13) || char(32)) = '免责声明：信息来源于官方媒体/网络新闻等，仅信息分享，不作为投资建议！'
      AND (node.parent IS NULL OR typeof(node.key) = 'integer' OR node.key = 'msg')
  ), 0) AS parsed_removable_matches,
  coalesce((
    SELECT count(*)
    FROM events AS e
    JOIN json_each(e.parsed_content_json, '$.texts') AS text
    WHERE text.type = 'text'
      AND trim(text.atom, char(9) || char(10) || char(11) || char(12) || char(13) || char(32)) = '免责声明：信息来源于官方媒体/网络新闻等，仅信息分享，不作为投资建议！'
  ), 0) AS extracted_text_matches;
```

## 日志、备份与手工恢复

仅在本机查看最近日志：

```bash
tail -n 200 logs/mx-listener.out.log
tail -n 200 logs/mx-listener.err.log
./.venv-runtime/bin/advisor-services status
```

备份前先停止 Listener 并确认状态不再显示有效租约。然后复制数据库；媒体目录交给正常的本机备份策略，不要移动文件或改写数据库中的关联。

```bash
./.venv-runtime/bin/advisor-services stop mx-listener
mkdir -p "$HOME/Documents/Ahunter-backups"
cp -p data/state/events.sqlite "$HOME/Documents/Ahunter-backups/events-$(date +%Y%m%d-%H%M%S).sqlite"
```

Mac 重启、登录失效或连接中断后的手工恢复步骤是：

1. 用 `advisor-services status` 读取三维状态；如果服务仍在运行，先用 WebUI 或 CLI 停止。
2. 用上面的无代理新 shell 运行离线自检；失败时保持停止状态并先修复。
3. 由用户在 WebUI 显式启动专用 Chrome，或按用户请求执行 `ahunter mx chrome start`；用户自行恢复登录状态和 MX 页面；不要让 Listener、脚本或启动器代替用户操作页面。
4. 从 WebUI 或 CLI 启动 Listener，并观察状态从等待转为 `listening`；如果仍在等待，保留服务运行即可。

若 `failed`、RID 配置不可读或租约长期不释放，保持服务停止，保存有限的状态和日志信息后再排查。不要删除旧 runtime 标记、浏览器 profile、事件、媒体、Agent/Team Manifest 或报告来“重置”。
