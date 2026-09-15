---
id: RE-009
status: done
depends_on: [RE-001, RE-002]
adrs: [0006, 0007, 0010, 0014, 0016]
---

# 实现本机 Codex Executor 与统一执行策略

## 结果

Research Engine 通过本机 Codex CLI 执行所有生成式任务，显式应用仓库版本化策略，并将每次 Attempt 的有效配置、状态和结果写入控制面。

## 范围

- 发现并校验可执行的 Codex CLI，检查本机 ChatGPT 会话可用性，失败时明确阻断。
- 以独立临时任务执行每个 Research Invocation 和每个 Decision Stage。
- 使用显式模型、推理强度、超时、并发、输出 Schema、工作目录及隔离参数；忽略用户默认配置。
- 禁用用户配置、插件、MCP、网络搜索和持久会话；子进程只继承最小环境白名单。
- 解析结构化输出与可用 usage/timing 信息，记录 Codex CLI 版本和所有有效策略字段。
- 只对超时、进程失败和 Schema-invalid 输出自动重试一次；复用相同 Capsule，接受首个有效结果。
- 支持取消、全局并发限制、标准化错误分类和有界日志。
- 使用 fake Codex executable 完成绝大多数自动化测试；另提供显式手动 smoke 命令。

## 不包含

- Provider-specific 模型客户端。
- Agent/Team 自行选择模型。
- 因结论弱、质量低或观点不理想而重试。

## 验收条件

- [x] 缺少可执行文件、本机会话不可用、模型不可用和超时均产生明确的阻断状态。
- [x] 每个任务使用新的 ephemeral 进程，且没有复用对话历史。
- [x] Agent/Team Manifest 中出现执行策略覆盖字段时 Catalog 校验失败。
- [x] 首个 Schema-valid 输出被接受后不会继续采样或择优。
- [x] 两次技术失败后 Invocation/Stage 失败，所有 Attempt 独立可审计。
- [x] 子进程无法读取 Capsule 外 sentinel，且测试证明用户默认配置不会改变有效策略。
- [x] `codex-v1` 填写明确的模型、推理强度、超时和并发值，不允许空值或继承默认；手动 smoke 证明配置在当前安装中可用。
- [x] 执行策略内容变化会形成新版本并改变运行指纹。

## 可能触点

- `advisor/research/codex/executor.py`
- `advisor/research/codex/policy.py`
- `config/research/execution/codex-v1.yaml`
- `tests/advisor/research/test_codex_executor.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_codex_executor.py
```
