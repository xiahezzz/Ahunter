---
id: RE-001
status: done
depends_on: []
adrs: [0002, 0003, 0004, 0011, 0014, 0015]
---

# 建立领域契约与 Manifest Catalog

## 结果

建立 `advisor.research` 的稳定公共接口，以及只从仓库固定目录发现 Data Product、Agent、Team、Decision Pipeline 和 Codex Execution Policy 定义的 Catalog。后续模块只依赖这些领域对象，不依赖 YAML 字典或外部框架状态。

## 范围

- 定义不可变、可序列化的标识、版本、Subject、时间边界、Manifest 引用和运行状态类型。
- 定义 Research Finding 的公共核心：Agent 身份、Subject、`as_of`、摘要、带证据引用的 claims、风险、失效条件、质量信息和 Agent 专属 details。
- 定义 Agent Manifest、Team Manifest、Data Product Manifest、Decision Pipeline Manifest 和 Codex Execution Policy 的加载模型。
- Catalog 只扫描仓库约定目录；精确匹配 ID 与版本，并生成稳定排序结果。
- 支持声明式 Agent；保留 A Hunter 自有 code-backed Agent 的窄接口，但不加载仓库外 entry point。
- 在启动阶段验证重复 ID、未知引用、循环引用、非法 Schema、Team 中重复成员及不允许的覆盖字段。

## 不包含

- 数据抓取、Codex 子进程、状态机或报告生成。
- 任何运行时临时 Agent 列表。
- Team 自定义模型、提示参数、Decision Pipeline 或风险规则。

## 验收条件

- [x] 所有 Manifest 模型均拒绝未知字段，序列化结果稳定。
- [x] Team Manifest 只能精确引用 Agent ID 与版本；成员或版本变化会形成不同 Team 定义。
- [x] Agent Manifest 只能声明自身指令、Data Product 依赖、查询预算和 Finding details Schema。
- [x] 同一个 Catalog 输入无论文件遍历顺序如何都产生相同结果。
- [x] 重复、缺失、越界和仓库外实现引用在启动时 fail closed，并给出不包含文件内容的错误。
- [x] 单元测试覆盖成功目录、所有主要失败分支及不可变性。

## 可能触点

- `advisor/research/contracts.py`
- `advisor/research/catalog.py`
- `config/research/{products,agents,teams,pipelines,execution}/`
- `tests/advisor/research/test_catalog.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_catalog.py
```
