---
id: MD-004
status: complete
depends_on: [MD-001]
adrs: [0013, 0030, 0032, 0035, 0043]
---

# 实现东方财富主行情适配器

> 历史记录：本实现已被 MD-018 与 ADR-0057 取代，不属于当前 Market Daily 运行路径。

## 结果

仓库自有 Eastmoney Adapter 能按代码和闭区间返回完整、未复权、单位统一且可直接校验的 Canonical Daily Bars，覆盖五年请求而不依赖旧新浪实现。

## 范围

- 定义窄的 `DailyBarProvider` 契约和 Eastmoney HTTP 实现，显式传入代码、开始日和结束日。
- 请求未复权日线并解析日期、OHLC、成交量和成交额；拒绝缺少任何必需字段的记录。
- 把来源成交量统一换算为整数股，金额统一为人民币元，并记录来源定位、抓取时间和内容哈希。
- 严格检查 HTTP 状态、内容类型、JSON 形状、代码一致性、日期升序唯一、请求边界和未来数据。
- 支持完整五年区间与小型增量区间；Provider 本身不写数据库。
- 将 endpoint、超时和并发限制作为普通运行配置，解析器不依赖网络状态。
- 移除 Sina 作为五年历史必需路径的假设，不再用 `amount=0` 补字段。

## 不包含

- TDX、跨来源回退、重试状态机、复权计算或股票池。
- 实时联网单元测试。

## 验收条件

- [x] 五年 fixture 能返回超过 800 根日线且首尾日期正确。
- [x] 成交量“手”正确换算为“股”，成交额不允许伪造为 0。
- [x] 重复日期、乱序、非法 OHLC、非有限数值、错误代码和未来行全部被拒绝。
- [x] 相同响应生成相同 Canonical Daily Bar 哈希。
- [x] Provider 不导入 TradingAgents、`mootdx` 或旧全局数据路由。
- [x] 自动化测试不访问外部网络。

## 可能触点

- `advisor/market_daily/providers/contracts.py`
- `advisor/market_daily/providers/eastmoney.py`
- `config/data-sources.yaml`
- `tests/advisor/market_daily/fixtures/eastmoney/`
- `tests/advisor/market_daily/test_eastmoney_provider.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/market_daily/test_eastmoney_provider.py
```
