---
id: RE-008
status: done
depends_on: [RE-002, RE-003]
adrs: [0005, 0010, 0023]
---

# 实现 Run Capsule 与只读 Research Query Interface

## 结果

每个 Agent 或 Decision Stage 获得一个最小、不可变、可审计的工作目录。Research Agent 可以主动检索自身已声明的 Snapshot 子集，但无法看到仓库、其他 Agent 数据、整个 Artifact Store 或实时网络。

## 范围

- 物化包含任务指令、Subject、时间边界、允许输入、证据索引、输出 Schema 和版本指纹的 Run Capsule。
- 为 Research Agent 提供受限本地查询命令，支持产品枚举、过滤、文本检索、时间序列切片和允许的确定性聚合。
- 使用只读数据库/文件视图和参数化查询；拒绝写入、附加数据库、任意文件路径和未声明 Product ID。
- 对每次查询记录规范请求、参数、返回条数、结果哈希、耗时和预算消耗。
- 在 Agent Manifest 中执行查询次数、返回行数和总字节预算。
- Decision Stage Capsule 只包含规定的上游 Finding/Review，不自动获得 Research Query Interface。
- Capsule 生命周期结束后可清理临时副本，但其 Manifest、内容哈希和查询日志进入 Artifact Store。

## 不包含

- 运行时抓取新数据。
- Agent 私有长期记忆。
- 用户本机配置、插件、MCP 或仓库级工具。

## 验收条件

- [x] Agent 能查询声明产品并得到稳定结果；相同查询与输入产生相同结果哈希。
- [x] 查询未声明产品、写入数据、读取绝对路径、越过 Capsule 或超过预算均 fail closed。
- [x] 安全测试中的 Capsule 外 sentinel 文件不可读取。
- [x] 一个 Team/Agent 的 Capsule 不包含其他 Team 身份或未声明产品。
- [x] 查询日志足以复现返回集，并与对应 Invocation 关联。
- [x] 清理临时目录不会删除 Artifact Store 中的审计记录。

## 可能触点

- `advisor/research/capsules.py`
- `advisor/research/query.py`
- `advisor/research/query_cli.py`
- `tests/advisor/research/test_capsules.py`
- `tests/advisor/research/test_query_interface.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_capsules.py tests/advisor/research/test_query_interface.py
```
