---
id: MD-001
status: complete
depends_on: []
adrs: [0029, 0030, 0031, 0034, 0035, 0036, 0039, 0041, 0043]
---

# 建立行情领域契约与 SQLite 基础表

## 结果

建立独立于 Provider 和运行入口的 Market Daily 公共契约，并在现有 `advisor.sqlite` 中定义极简、可校验的沪深证券、原始日线、复权因子、交易日和合法缺席表。

## 范围

- 建立 `advisor.market_daily` 包及不可变的 `MarketSecurity`、`CanonicalDailyBar`、`AdjustmentFactor`、`ObservedTradingSession` 和 `MarketAbsence` 类型。
- 六位代码是唯一证券身份；只接受沪市 `6xxxxx` 与深市 `0xxxxx`、`3xxxxx` 普通股，证券类型由股票池来源确认而非只靠正则猜测。
- 直接修改基线 Schema，建立或收敛 `securities`、`market_daily`、`market_adjustment_factors`、`trading_sessions`、`market_daily_absences`。
- `market_daily` 以 `(code, trade_date)` 唯一；价格为元/股、`volume` 为整数股；`amount` 有来源时为人民币元，新浪历史日 K 未提供时为 `NULL`，不得伪造为 0；表内不再存放复权因子或通用版本历史。
- 每条持久化事实保留来源、来源时间、抓取时间、`as_of`、Schema 版本、内容哈希和质量状态。
- 对 Canonical Daily Bar 执行有限数值、正价格、OHLC 边界、非负成交量、可选成交额、日期边界和稳定内容哈希校验。
- 相同主键和内容是幂等；相同主键不同内容返回显式冲突，不覆盖原值。
- `market_daily_absences` 只保存有证据的停牌等例外，不复制正常交易、未上市或已退市状态。

## 不包含

- 网络请求、股票池解析、冷启动状态机或 Service 进程。
- 旧行情数据清理或兼容迁移。
- Research Engine 查询和 WebUI。

## 验收条件

- [x] 所有领域类型拒绝未知字段和非法单位，序列化与哈希稳定。
- [x] Schema 外键、唯一约束和索引覆盖按代码日期查询、最新覆盖查询和异常缺席查询。
- [x] 同值重复写入返回 no-op；不同值写入返回冲突且数据库旧值不变。
- [x] 复权因子与原始 K 线物理分离。
- [x] 行情、因子、交易日和合法缺席均能追溯到来源与抓取时间。
- [x] 不存在北交所、B 股、基金、债券或指数专用身份分支。
- [x] Schema 测试从空数据库创建全部表并验证列类型、约束和 WAL 设置。

## 可能触点

- `advisor/market_daily/contracts.py`
- `advisor/market_daily/repository.py`
- `advisor/db/schema.sql`
- `advisor/db/repository.py`
- `tests/advisor/market_daily/test_contracts.py`
- `tests/advisor/market_daily/test_schema.py`
- `tests/advisor/test_db_schema.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/market_daily/test_contracts.py \
  tests/advisor/market_daily/test_schema.py \
  tests/advisor/test_db_schema.py
```
