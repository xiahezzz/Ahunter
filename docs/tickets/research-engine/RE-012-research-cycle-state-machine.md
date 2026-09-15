---
id: RE-012
status: done
depends_on: [RE-002, RE-003, RE-010, RE-011]
adrs: [0008, 0012, 0016, 0019, 0021]
---

# 实现 Research Cycle 显式状态机

## 结果

一个 Subject/`as_of` 的 Research Cycle 能通过明确、持久化的阶段完成 Snapshot、唯一 Agent Invocations、每 Team Decision Pipeline 和发布准备；不依赖 LangGraph。

## 范围

- 定义 Cycle、Team Run、Invocation 和 Stage 的合法状态、转换及终态。
- 按选定 Teams 构建产品并集并封存 Snapshot。
- 按 Research Invocation Key 去重，以统一并发限制执行唯一 Agents。
- 对每个 Team 检查必需 Findings；完整 Team 独立进入 Decision Pipeline，不完整 Team 标记 blocked。
- 在 Snapshot sealed、Agents complete、每个 Stage complete 和 Team terminal 等幂等边界持久化状态。
- 重启时复用已验证的完成 Artifact，从最后完成边界继续；运行中的 Codex 进程一律视为未完成并按 Attempt 规则处理。
- 支持取消、重复请求幂等、局部失败和多个 Teams 的独立终态。

## 不包含

- 报告文件布局或每日多 Subject 编排。
- LangGraph checkpoint、外部图状态或 callback routing。
- Team 间结论处理。

## 验收条件

- [x] 状态转换表拒绝跳阶段、回退终态和重复发布。
- [x] 相同 Cycle ID 重放不重复抓取 sealed 产品、不重复接受 Finding、不覆盖结论。
- [x] 相同 Agent 被多个 Teams 使用时只执行一次；失败准确阻断所有依赖 Team。
- [x] 一个 Team blocked 不影响无依赖 Team 进入并完成 Pipeline。
- [x] 模拟每个持久化边界崩溃后均可安全恢复，且不尝试恢复活跃 Codex 进程。
- [x] Cycle 指纹包含所有 Team/Agent/Product/Pipeline/Execution Policy 版本和输入哈希。
- [x] 代码及依赖中不引入 LangGraph。

## 可能触点

- `advisor/research/engine.py`
- `advisor/research/state_machine.py`
- `advisor/research/repository.py`
- `tests/advisor/research/test_state_machine.py`
- `tests/advisor/research/test_cycle_recovery.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_state_machine.py tests/advisor/research/test_cycle_recovery.py
```
