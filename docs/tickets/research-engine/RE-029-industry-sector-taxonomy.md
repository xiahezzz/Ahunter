---
id: RE-029
status: done
depends_on: [RE-003, RE-023, MD-003]
adrs: [0083, 0084, 0085]
---

# 建立版本化一级行业分类快照

## 结果

板块研究使用独立于行情 Provider 的 repository-owned 东财一级行业成员快照；每次研究固定具体版本，并对刷新失败和陈旧版本做可审计降级。

## 范围

- 发布 `industry_sector_taxonomy@1` Data Product，规范化一级行业 ID、中文名称、成员代码、有效/观察时间、来源、抓取时间、覆盖率、版本与内容哈希。
- 使用东方财富一级行业及成分关系作为初始分类来源；排除概念、地域、风格、二三级行业和行情响应中临时携带的 provider label。
- 分类持久化为独立不可变版本，不把现有未维护的 `securities.industry` 当作权威来源，也不因新浪/东财行情 fallback 改变成员归属。
- 每个沪深交易日首次开始需要板块成员的 Market Research 时尝试刷新一次；成功版本供当日后续 Research Records 复用并固定。
- 当日刷新失败时，可使用最近成功且不超过五个交易日的版本，并在 Data Product/Report 中披露版本和年龄。
- 无版本或版本超过五个交易日时，仅把 sector-strength/rotation Insight 标记 blocked；独立 breadth/sentiment 与 macro-policy Insight 仍可继续。
- 校验重复成员、未知代码、空行业、跨边界时间、异常成员骤变和 universe coverage，并保留有限质量原因。

## 不包含

- 行情抓取、概念板、统一板块强度公式、Agent 排名方法或为证券主表补写可变 industry 字段。
- 每次 Research Run 重抓 taxonomy、无限陈旧 fallback 或行情源决定分类。

## 验收条件

- [ ] 同一输入产生稳定版本/哈希，同日后续运行复用相同版本且不再请求来源。
- [ ] 新交易日首次需求尝试刷新；失败后按 0～5 个交易日允许、超过 5 日阻断的边界准确执行。
- [ ] 切换 Whole-Market Snapshot Provider 不改变 pinned taxonomy 或行业成员。
- [ ] 概念/地域/多级行业、重复成员、未知证券和越界观察被拒绝或形成明确质量失败。
- [ ] Sector Insight 可单独收到 taxonomy blocked 原因，其他 Market Insights 不因此被强制阻断。
- [ ] 固定 fixtures 覆盖来源分页、成员标准化、版本持久化和重启复用，不访问真实网络。

## 可能触点

- `config/research/products/industry_sector_taxonomy.yaml`
- `advisor/research/providers/industry_taxonomy.py`
- `advisor/research/repository.py`
- `advisor/db/schema.sql`
- `tests/advisor/research/providers/test_industry_taxonomy.py`
- `tests/advisor/research/test_industry_taxonomy_readiness.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/providers/test_industry_taxonomy.py tests/advisor/research/test_industry_taxonomy_readiness.py'
```
