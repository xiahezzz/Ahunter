---
id: RE-005
status: done
depends_on: [RE-003]
adrs: [0012, 0013, 0018, 0024]
---

# 实现基本面 Data Products

## 结果

Fundamentals、Lockup 和 Hot Money Agents 能查询结构化的公司估值、财务报表、盈利预期、行业位置和股东活动信息，不再消费外部工具返回的自由文本。

## 范围

- 定义并版本化公司基本面快照、资产负债表、利润表、现金流量表、机构预期、行业横向背景和股东活动产品。
- 将当前公开 A 股来源中相关解析逻辑改写为 A Hunter Provider Adapters，不导入外部数据流模块。
- 保留公告/披露时间与报告期的区别；Snapshot 只能包含 `as_of` 时已经披露的信息。
- 统一币种、单位、百分比、季度/年度频率、缺失值和追溯调整语义。
- 为每个产品定义最小覆盖与可选字段；可选字段不可用时应给出质量标记，而不是伪造数值。
- 行业横向数据记录样本日期、行业分类版本及目标证券位置。

## 不包含

- 由模型估算缺失财务数字。
- 将不同来源字段无记录地拼接成一条报表。
- 投资结论或估值判断。

## 验收条件

- [x] 每个财务值都能追溯到来源、报告期、披露时间和单位。
- [x] 报告期早于 `as_of` 但披露时间晚于 `as_of` 的记录被拒绝。
- [x] 空报表、未覆盖字段、合法零值和来源失败能被区分。
- [x] 机构预期记录覆盖机构数、预测年度和抓取时间；过期预测按 Manifest 规则降级或阻断。
- [x] 行业比较与目标基本面共享一致的时间边界。
- [x] fixture tests 覆盖解析、归一化、来源 fallback、单位转换和未来数据拦截。

## 可能触点

- `advisor/research/providers/fundamentals/`
- `config/research/products/fundamentals/`
- `tests/advisor/research/providers/test_fundamental_products.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/providers/test_fundamental_products.py
```
