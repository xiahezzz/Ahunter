---
id: RE-013
status: done
depends_on: [RE-012]
adrs: [0008, 0009, 0017, 0022, 0027]
---

# 发布每个 Team 的独立报告

## 结果

每个 Research Cycle 生成一个审计清单和每 Team 独立目录；成功 Team 发布自己的 JSON/Markdown，blocked Team 只发布自身状态。任何文件都不比较或综合 Teams。

## 范围

- 实现以下逻辑布局：

```text
reports/<date>/<cycle_id>/
├── cycle.json
├── index.md
└── teams/
    └── <team_id>@<version>/
        ├── conclusion.json
        └── report.md
```

- blocked Team 目录使用明确 `status.json`/失败报告，不创建 `conclusion.json`。
- `cycle.json` 包含 Subject、`as_of`、Snapshot/版本指纹、Team 状态和 Artifact 引用。
- `index.md` 只列身份、状态和文件入口，不引用或解释 Team Conclusion。
- Team Markdown 展示自身 Findings、Decision Reviews、最终结论、证据、风险、失效条件和质量状态。
- 使用现有安全原子发布约束：路径限制、不可覆盖、完成标记及验证读取。

## 不包含

- comparison 文件、共识、排名、一致/分歧叙述或跨 Team 章节。
- blocked Team 的部分建议。
- 再次调用 Codex 生成报告文案。

## 验收条件

- [x] 每个成功 Team 的 JSON 严格符合 Team Conclusion Schema，Markdown 由该 JSON 确定性渲染。
- [x] `index.md` 测试确保不包含 stance、conviction、thesis 或其他结论字段。
- [x] blocked Team 没有结论文件，失败原因关联具体缺失 Product/Invocation/Stage。
- [x] 文件名与路径由已验证 ID 构造，拒绝穿越、符号链接替换和原地覆盖。
- [x] 完成标记出现前 JSON、Markdown 和 Artifact 引用均已落盘并通过哈希校验。
- [x] 搜索输出目录不存在 comparison/consensus/ranking 类型产物。

## 可能触点

- `advisor/research/reporting/team.py`
- `advisor/research/reporting/cycle.py`
- `advisor/reporting/contracts.py`
- `tests/advisor/research/test_team_reporting.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_team_reporting.py tests/advisor/test_reporting.py
```
