---
id: RE-015
status: done
depends_on: [RE-014]
adrs: [0003, 0009, 0022, 0023, 0028]
---

# 将投研投影与收盘复盘按 Team 隔离

## 结果

主观投研状态不会写入全局 `stock_profiles` 形成最后写入者覆盖；22:30 review 能对当天每个 Team/Subject 的已发布结论进行独立、确定性的结果评估，保持现有“晨报—晚间复盘”链路，但不比较 Teams，也不把复盘隐式注入未来 Agents。

## 范围

- 将证券身份、客观行情、图表和账本暴露保持为共享事实；将 thesis、Agent flow、Team Conclusion 历史等主观投研投影改为 Team ID/version + Subject 作用域，或直接从不可变 Team Artifacts 派生。
- 所有 advice/conclusion/review 记录携带精确 Team ID/version，禁止不同 Team 更新同一主观 profile 行。
- Review 精确引用原 Team Conclusion ID、Team 版本、Subject、晨间 `as_of` 和报告 Artifact。
- 使用截止 review `as_of` 的行情、持仓与交易记录评价 stance 后的可观察结果和执行约束。
- 每个 Team 单独输出 review JSON/Markdown；Daily review brief 只整理该 Team 的 Subject reviews。
- 保留数据质量门：缺少有效收盘、晨间结论或精确关联时，该 review blocked。
- 复盘产物进入 Artifact Store，可供人工改进 Agent/Pipeline 版本，但默认不进入未来 Run Capsule。
- 调整现有 advice/review 领域映射，移除对数值 confidence 的依赖。

## 不包含

- Team 胜率排名、风格比较或自动 Champion/Challenger。
- 根据复盘自动修改 Agent instructions。
- 真实交易执行。

## 验收条件

- [x] 每条 Review 只能关联一个已验证的 Team Conclusion，跨 Team/Subject 关联被拒绝。
- [x] 两个 Teams 对同一 Subject 的运行不会互相覆盖 thesis、Agent flow、Conclusion 或 review history。
- [x] 共享证券事实和 chart Artifact 可由多个 Teams 引用，但不复制或混入 Team 主观字段。
- [x] 同一 Team 的 review 输出中不出现其他 Team 数据。
- [x] 没有可靠行情或质量门失败时不生成绩效判断。
- [x] Review 不创建新的 Decision Stance，也不调用 Codex。
- [x] 现有晨报链接、账本匹配和不可覆盖归档安全测试适配新 ID 后继续通过。
- [x] 下一日 Agent Capsule 测试证明不会自动包含 review 内容。

## 可能触点

- `advisor/coordinator.py`
- `advisor/profiles/service.py`
- `advisor/reporting/review.py`
- `advisor/research/reviews.py`
- `advisor/db/schema.sql`
- `tests/advisor/research/test_team_reviews.py`
- `tests/advisor/research/test_team_profiles.py`
- `tests/advisor/test_reporting.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_team_profiles.py tests/advisor/research/test_team_reviews.py tests/advisor/test_reporting.py tests/advisor/test_coordinator.py
```
