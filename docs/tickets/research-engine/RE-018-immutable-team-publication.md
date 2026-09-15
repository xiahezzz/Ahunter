---
id: RE-018
status: done
depends_on: [RE-001, RE-010]
adrs: [0003, 0011, 0015, 0053, 0054, 0055]
---

# 发布不可变 Team Manifests

## 结果

Research Engine 提供一个项目内 Team Publication 服务，将 WebUI 提交的稳定 Team ID、中文名称和 Agent 身份发布成仓库内不可变、自动递增版本的 YAML Manifest；发布不创建 Research Run，也不修改每日启用集合。

## 范围

- 发布输入只包含稳定 Team ID、中文名称和非空 Agent ID 集合，不接受 Team 版本或 Agent 历史版本。
- 每次发布重新加载并校验仓库 Catalog，将每个 Agent ID 解析为当前最新 Manifest 版本，再把精确引用写入 Team Manifest。
- 新 Team 从 `@1` 开始；同一 Team ID 的修订在串行化边界内分配下一个整数版本，并以独占、原子方式创建 `config/research/teams/<team_id>_v<version>.yaml`。
- 完全相同的重复发布请求幂等返回已有最新版本，不产生空洞版本；不同请求永不覆盖已有文件。
- 允许任意非空 Agent 组合，不设置必选 Agent，也不计算证据覆盖提示。
- 不同稳定 Team ID 若拥有相同 Agent ID 集合则拒绝发布；同一 Team 内重复 Agent 同样拒绝。
- 发布成功后重新加载 Catalog 即可发现新版本，不依赖进程重启或 Python 中央注册表修改。
- 已发布 Team 没有修改、删除或归档操作，历史版本永久保留。

## 不包含

- Web API、WebUI、每日启用状态或 Research Run 启动。
- Agent Manifest、instructions、Data Product 或 Decision Pipeline 的在线编辑。
- SQLite Team 定义表、持久化草稿、插件目录或仓库外 Team 来源。

## 验收条件

- [x] 首次发布产生 `team_id@1`，修订自动产生连续版本，调用方不能指定或覆盖版本号。
- [x] Manifest 精确固定发布时各 Agent 的最新版本；新增 Agent 版本不改变任何既有 Team。
- [x] 空 Agent 集合、未知 Agent、重复 Agent、非法 ID 和跨 Team 重复成员组合均 fail closed，且不留下文件。
- [x] 完全相同的重试返回同一 Team 引用，并发双击不会创建两个等价版本或半写文件。
- [x] 新文件通过完整 Catalog 校验并可立即读取；SQLite 中不存在第二份 Team 定义来源。
- [x] 自动化测试证明发布路径不调用 Provider、Codex、Decision Pipeline 或报告器。

## 可能触点

- `advisor/research/contracts.py`
- `advisor/research/catalog.py`
- `advisor/research/team_publication.py`
- `config/research/teams/`
- `tests/advisor/research/test_team_publication.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_catalog.py tests/advisor/research/test_team_publication.py
```
