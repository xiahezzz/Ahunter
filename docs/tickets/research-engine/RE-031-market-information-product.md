---
id: RE-031
status: done
depends_on: [RE-003, RE-023]
adrs: [0005, 0023, 0094]
---

# 实现全市场宏观政策信息产品

## 结果

`market_macro_policy@1` 获得一个不依赖股票代码的、边界安全且可溯源的 Market Information Product，以官方原始发布为主、财经媒体为补充。

## 范围

- 发布 `market_information@1` Data Product，覆盖宏观经济、货币财政政策、监管、交易所规则、统计发布和重大全市场事件。
- 官方机关、监管机构、交易所和统计机构的原始发布具有最高来源层级；财经媒体仅补充事件发现和市场语境，有原始发布时 evidence 必须指向原文。
- 规范化字段至少包含稳定 event ID、类别、标题、有限摘要、publisher、source tier、published/observed/fetched time、source locator、内容哈希和去重关系。
- 不复用现有按 Security code 关键词请求的 `macro_news@1`；Market Subject 请求不接受或注入股票代码。
- 所有记录必须在 Research Boundary 内，冲突时间、未知发布时间和来源不可追溯按版本化质量规则处理。
- 同一原始发布被多个媒体转载时保留一个 canonical event 和可审计 references，不把转载数量当作证据强度。
- 未经证实的传闻不能进入事实 claims；可验证的媒体报道必须明确标记来源层级与限制。
- Agent 只能通过已声明产品/Capsule 查询，不能自行联网搜索或绕过 Provider provenance。

## 不包含

- 个股新闻、社交情绪、MX 授权 feed 扩张、网页全文长期存档、Agent 影响判断或实时新闻常驻爬虫。
- 要求付费数据源、API key 或把财经媒体升级为官方来源。

## 验收条件

- [ ] Market Subject 不触发股票代码搜索，Security-only `macro_news@1` 行为保持不变。
- [ ] official-first 选择、媒体补充、原文优先引用、转载去重和来源层级均有固定 fixtures。
- [ ] Boundary 后发布、无法确定时间、无来源和传闻 fixture 不会成为通过质量门的事实 evidence。
- [ ] Provider 失败只使 macro-policy Insight 不可用，不污染 breadth 或 sector 产品。
- [ ] payload、provenance、retention metadata 和内容哈希可进入 sealed Snapshot 并被 Agent Finding 引用。
- [ ] 单元测试完全离线，不访问真实官方网站或财经媒体。

## 可能触点

- `config/research/products/market_information.yaml`
- `advisor/research/providers/information.py`
- `advisor/research/providers/public.py`
- `advisor/research/data_products/engine.py`
- `tests/advisor/research/providers/test_information_products.py`
- `tests/advisor/research/test_public_providers.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/research/providers/test_information_products.py tests/advisor/research/test_public_providers.py'
```
