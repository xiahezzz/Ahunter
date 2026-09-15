---
id: MX-011
status: complete
depends_on: [MX-006, MX-010]
adrs: [0015, 0053, 0055, 0065, 0067]
---

# 建立 Agent 数据访问与 Team 研究配置 WebUI

## 结果

“研究配置”页面同时提供只读 Data Product Catalog、版本化 Agent Data Access 编辑和既有 Team 配置，用户能清楚看到“什么 Agent 能访问什么”，且所有变化都通过显式发布产生新版本。

## 范围

- 在研究配置页加入 Agent Data Access 区域，并保留现有 Team Publication/Daily Team Set 行为；两者共享目录刷新但草稿与错误互相隔离。
- Product Catalog 展示产品标题、精确版本、只读 Provider provenance 和依赖；依赖明确标为“构建需要，Agent 不自动可见”。
- Agent 卡按稳定 ID 展示最新版本、Data Products、MX RID Feeds、Team 使用情况和折叠历史版本。
- “基于此版本修订访问”创建仅存在页面内存的 Agent Access Draft；只允许增删 Product 与产品特定 scope。
- 指令、标题、输出契约、查询预算和实现信息显示为只读摘要；不提供文本框、JSON 编辑器或高级绕过入口。
- 任意 Agent 可选择任意已发布 Product，不显示角色白名单或覆盖不足警告；每项始终固定精确 Product 版本。
- 选择 `mx_events@2` 时必须逐项勾选当前授权 RID；没有“全部”按钮，不自动选中新 RID；已撤销历史引用只读显示并阻止原样发布。
- 发布期间禁止重复提交；成功显示 exact `agent@version` 并刷新目录，但不自动修改 Team 或 Daily Team Set。
- 对仍固定旧 Agent 的 Team 显示“有新 Agent 版本”及“基于此版本修订 Team”入口；只预填现有 Team Draft，仍需用户显式发布和每日启用。
- conflict、Catalog 变化、RID 配置变化、授权撤销、原子失败和“成功但刷新失败”均保留草稿并给出有限中文反馈。
- 离开页面、取消或刷新丢弃未发布 Draft；不写 localStorage、SQLite 或服务器草稿文件。

## 不包含

- 新建/删除 Agent、编辑 prompt/schema/budget/code、修改 Product/Provider、自动 Team 升级、启动 Research Run 或比较 Team。
- 用户/角色/审批、远程协作、通用权限管理或配置推荐。

## 验收条件

- [x] 用户可查看每个 Agent 最新/历史版本的 exact Data Products、Provider provenance 和 RID scope。
- [x] Draft 只改变 Data Access；发布请求不包含可编辑的 instructions/schema/budget/implementation。
- [x] 任意 Product 可选，依赖不自动勾选为 Agent 权限；MX scope 只能逐个选择当前授权 RID。
- [x] 新增 RID 显示未分配且不改变旧 Agent；撤销 RID 显示阻断并提供重新授权或发布移除 Feed 两种恢复提示。
- [x] 发布新 Agent 不改变 Team；Team 修订与 Daily 启用仍为两个显式步骤。
- [x] 草稿生命周期、历史折叠、幂等、conflict、刷新失败、malformed API 和键盘/窄屏均有前端测试。
- [x] 页面与请求中不暴露 Provider endpoint、原始 MX 数据、真实配置文件内容或内部路径。

## 可能触点

- `frontend/src/research/AgentAccessPanel.tsx`
- `frontend/src/research/DataProductCatalog.tsx`
- `frontend/src/research/TeamsPanel.tsx`
- `frontend/src/research/contracts.ts`
- `frontend/src/research/*.test.tsx`
- `frontend/src/styles.css`
- `frontend/src/App.test.tsx`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node frontend/node_modules/vitest/vitest.mjs run --root frontend src/research --reporter=dot'
```

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c '/Users/mac/.local/share/chrome-devtools-mcp/node/bin/node frontend/node_modules/typescript/bin/tsc --noEmit -p frontend/tsconfig.json'
```
