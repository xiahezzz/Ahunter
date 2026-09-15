---
id: RE-003
status: done
depends_on: [RE-001, RE-002]
adrs: [0005, 0012, 0013, 0018]
---

# 建立 Data Product Engine 与 sealed Snapshot

## 结果

Research Engine 能根据选定 Teams 计算 Data Product 依赖并一次性构建不可变 Snapshot。Agents 不认识来源实现，只消费 Manifest 声明的规范化产品。

## 范围

- 定义窄接口：Product Request、Provider Observation、Provider Adapter、产品质量结果和 Snapshot Builder。
- Data Product Manifest 描述请求/结果 Schema、时间语义、新鲜度、最小覆盖、可选字段、来源策略、冲突容差、保留规则及派生产品依赖。
- 计算所有选定 Teams 的产品并集，校验产品依赖 DAG，按依赖顺序物化一次。
- 按产品策略执行来源尝试、Schema 归一化、质量检查、fallback 和来源记录。
- 当多个参与判定的观察结果超出产品容差时生成 Conflicted Data Product，并阻断依赖它的 Teams。
- Snapshot seal 后禁止原地刷新；每个 Agent 只能获得自身 Manifest 声明的产品子集。
- 区分“合法空结果”“可接受的部分覆盖”“缺失产品”和“冲突产品”。

## 不包含

- 具体 A 股网络来源。
- Agent 查询体验、提示词或模型执行。
- 静默平均、字段级拼接或由模型选择来源。

## 验收条件

- [x] 同一 Cycle 的相同产品只物化一次，并被所有依赖 Team/Agent 引用。
- [x] 一个产品失败只阻断依赖它的 Teams，其他 Teams 可继续。
- [x] 每次来源尝试、选中来源、时间、质量和内容哈希均可审计。
- [x] 产品晚于 `as_of`、不满足新鲜度或最小覆盖时不能进入 sealed Snapshot。
- [x] fallback 返回不同来源时仍满足同一 Product Schema。
- [x] seal 后任何修改都会创建新 Snapshot，而不是改变原对象。
- [x] 使用 fake adapters 覆盖成功、fallback、冲突、合法空结果、部分覆盖和超时测试。

## 可能触点

- `advisor/research/data_products/contracts.py`
- `advisor/research/data_products/engine.py`
- `advisor/research/data_products/snapshot.py`
- `tests/advisor/research/test_data_product_engine.py`
- `tests/advisor/research/test_data_product_engine.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/research/test_data_product_engine.py
```
