---
id: MD-005
status: complete
depends_on: [MD-001]
adrs: [0013, 0032, 0043, 0045]
---

# 实现 TDX 完整备源适配器

> 历史记录：本实现已被 MD-018 与 ADR-0057 取代，不属于当前 Market Daily 运行路径，基础运行依赖也已移除。

## 结果

仓库通过 `tdxpy` 直接接入 TDX TCP 协议，提供与 Eastmoney 完全相同的 Canonical Daily Bar 契约，并能在主源失败时整条记录回退。

## 范围

- 只引入轻量 `tdxpy` 依赖，封装连接、服务器探测、分页历史日线、超时和关闭行为。
- TDX Adapter 返回日期、OHLC、整数股成交量与人民币成交额，执行与主源相同的校验和哈希规则。
- 支持五年分页读取与单日/缺失区间读取，合并分页时拒绝重复、乱序和间隙外数据。
- 建立有序 Provider Chain：Eastmoney 主源满足完整契约即停止；失败后才调用 TDX，禁止字段级拼接。
- 为主源两次、备源一次的调用预算提供可测试接口；实际 Run 状态由 MD-008 管理。
- 把连接错误收敛为有限、可记录的 Provider 错误，不泄漏服务器内部调试信息。
- 验证安装后 `httpx>=0.27,<1` 仍满足项目开发环境，不安装 `mootdx`。

## 不包含

- 冷启动状态机、常驻 Service、WebUI 或自动服务器无限重试。
- 北交所市场协议。

## 验收条件

- [x] TDX fixture 与 Eastmoney fixture 规范化后满足同一数据契约和单位。
- [x] 主源成功时备源调用次数为 0；主源失败后只接受完整备源结果。
- [x] 任一来源缺少成交额时整条观察失败，不从另一来源补字段。
- [x] 分页边界、重复页、空页和连接中断均有离线测试。
- [x] `pyproject.toml` 与干净环境解析不包含 `mootdx`，且 `httpx` 版本约束不回退。
- [x] 所有 TCP 行为通过 fake client 测试，不访问实时服务器。

## 可能触点

- `advisor/market_daily/providers/tdx.py`
- `advisor/market_daily/providers/registry.py`
- `pyproject.toml`
- `tests/advisor/market_daily/test_tdx_provider.py`
- `tests/advisor/market_daily/test_provider_chain.py`

## 验证

```bash
./.venv311/bin/python -m pytest \
  tests/advisor/market_daily/test_tdx_provider.py \
  tests/advisor/market_daily/test_provider_chain.py \
  tests/advisor/test_web_api.py
```
