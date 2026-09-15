---
id: RE-006
status: done
depends_on: [RE-003]
adrs: [0005, 0012, 0017, 0018, 0024]
---

# 实现新闻、政策与事件 Data Products

## 结果

News、Policy 和 Social Agents 能在统一时间边界内查询公司信息、市场/宏观信息和已通过质量检查的本地 MX 事件，同时保留来源与发布时间。

## 范围

- 定义并版本化公司信息流、市场/宏观信息流和本地 MX 事件产品。
- 将公开 A 股公司新闻与财联社等宏观信息解析成统一 item：标题、允许保留的摘要、发布时间、证券关联、分类、来源定位和质量状态。
- 复用 `advisor.evidence` 已归一化事件边界；只读取 allowlisted、成功解码且未被质量门阻断的数据。
- Social Agent 以信息流和 MX 事件为证据分析市场讨论，不把产品层的推断伪装成原始情绪事实。
- Policy Agent 使用相同产品中的明确政策类别与来源层级，不在 Provider 层生成政策结论。
- 实施去重、时间截断、条数/字符上限和来源保留规则。

## 不包含

- Codex 联网搜索或实时补取。
- 自动扩展 RID、操作浏览器页面或改变 collector 行为。
- 保存未经许可的完整文章正文。

## 验收条件

- [x] 晚于 `as_of` 的条目不进入 Snapshot；发布时间缺失或无效时按产品质量规则处理。
- [x] 重复转载可追溯到各来源，但 Agent 查询结果不会无界重复相同内容。
- [x] MX collector 的阻断状态会阻止其产品进入 Snapshot，且不会产生研究结论。
- [x] Social、News、Policy 可共享相同底层 Artifact，同时只看到各自声明的查询视图。
- [x] 条目上限、文本上限和排序稳定，输入顺序变化不影响内容哈希。
- [x] 所有解析测试使用脱敏 fixture，不访问实时网络或浏览器。

## 可能触点

- `advisor/evidence/service.py`
- `advisor/research/providers/information/`
- `config/research/products/information/`
- `tests/advisor/research/providers/test_information_products.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/test_mx_evidence_quality.py tests/advisor/research/providers/test_information_products.py
```
