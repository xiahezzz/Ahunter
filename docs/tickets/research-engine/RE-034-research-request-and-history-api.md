---
id: RE-034
status: done
depends_on: [RE-024, RE-025, RE-026, RE-027, RE-033]
adrs: [0072, 0080, 0090, 0091, 0092, 0093, 0097, 0098, 0099]
---

# 提供 Research Request、状态与历史 API

## 结果

Local Operator Console 拥有一组稳定的本机 API，可立即提交任意精确已发布 Team 版本、读取全局队列与进度、取消/再次研究，并分页查询所有来源的历史记录和报告。

## 范围

- 提供提交端点：输入精确 `team@version`、Scope-compatible Subject 和 submission identity，校验后持久化 Request，返回 `202` 与 Request ID；HTTP handler 不执行 Provider、Codex 或后台 task。
- Team Scope 决定请求形状：Market Team 请求不接受股票代码；Security Team 请求必须且只接受一个合法六位沪深代码。
- 任意历史 published Team version 均可选择；draft、不存在版本、跨 Scope Subject 和临时 Agent 列表均拒绝。
- 提供全局 current queue/status API，按实际执行顺序返回唯一 running 与全部 queued Records、Service liveness、阶段、Agent x/y、Decision Stage、时间和可取消能力。
- 提供 queued/running cancel 端点与 terminal rerun 端点；rerun 新建 Request，复制精确 Team/Subject 并返回新 ID。
- 提供 Research Record 分页 API：默认 passed/partial，支持 stable Team ID、可选 exact version、状态过滤；按 publication time 倒序，无 recent-100 上限。
- Record detail 返回 exact Team、Scope、Subject、origin、requested/accepted/boundary/published times、quality、partial blocked items、有限停止原因和经哈希验证的 report Markdown/JSON 引用。
- 统一呈现 WebUI、scheduled、CLI 与 legacy origins；数据库或 Service 不可用时返回真实 503/degraded 状态，不伪造空队列或成功。
- API 不返回 raw logs、provider bodies、prompts、private reasoning、Codex session material、内部路径或 unrestricted artifacts。

## 不包含

- 前端页面、远程访问、用户权限、WebSocket、同步等待长研究或删除历史记录。
- 按 Agent 筛选、首页默认 Team、每天一次限制或允许浏览器提交 Boundary。

## 验收条件

- [ ] Market 与 Security 提交形状、历史精确 Team 版本、非法 Scope/代码和 idempotent transport retry 均有 API 测试。
- [ ] 同一 Team/Subject 的两个独立提交产生两个 ID；handler 返回前没有 Provider/Codex 调用。
- [ ] current API 保持真实 queue order，Service offline 时 queued work 仍可见且状态不冒充 running。
- [ ] cancel 与 rerun 权限由当前状态决定；终态取消和非终态 rerun 返回稳定错误且不改原记录。
- [ ] 超过 100 条、跨 Team 版本、全 origins 和 status filters 的分页无重复/遗漏，排序稳定。
- [ ] Markdown/JSON hash 不匹配、路径逃逸、原始日志字段和敏感执行材料均 fail closed。

## 可能触点

- `advisor/web/api.py`
- `advisor/research/repository.py`
- `advisor/research/service.py`
- `tests/advisor/test_research_web.py`
- `tests/advisor/test_web_api.py`
- `tests/advisor/research/test_control_plane.py`

## 验证

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY='*' no_proxy='*' \
  /bin/zsh -f -c './.venv311/bin/python -m pytest tests/advisor/test_research_web.py tests/advisor/test_web_api.py tests/advisor/research/test_control_plane.py'
```
