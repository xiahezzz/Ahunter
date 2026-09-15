---
id: RE-002
status: done
depends_on: [RE-001]
adrs: [0015, 0016, 0017]
---

# 建立 SQLite 控制面与 Research Artifact Store

## 结果

用 SQLite 持久化研究生命周期和产物索引，用本地内容寻址存储保存不可变的规范化 JSON、文本和二进制产物。数据库行与文件通过内容哈希关联，报告可追溯到精确输入与执行版本。

## 范围

- 为 Daily Research Batch、Research Cycle、Research Run、Snapshot、Data Product、Research Invocation、Invocation Attempt、Decision Stage、Team Conclusion 和发布产物增加持久化结构。
- 实现规范 JSON 编码、SHA-256 地址、原子写入、重复内容复用、读取时校验和路径约束。
- 保存完整版本指纹、来源、时间边界、质量状态、尝试状态、父子关系和 Artifact 引用。
- 将大内容放入 Artifact Store，SQLite 只保存生命周期、查询字段、哈希和相对位置。
- 支持来源条款对应的保留元数据；内容不可用后保留明确状态和原始哈希。
- 提供事务边界，避免数据库引用尚未完成或校验失败的文件。

## 不包含

- Provider 抓取、Codex 执行或报告渲染。
- 覆盖或删除已存在的用户运行数据。
- 将运行产物提交到 Git。

## 验收条件

- [x] 相同字节只产生一个 Artifact，重复写入幂等。
- [x] 同一路径不能被不同内容覆盖；哈希不符、截断文件和路径穿越均 fail closed。
- [x] 写入中断不会留下可见的“完成”Artifact 或悬空成功记录。
- [x] Invocation 的每次 Attempt 独立留痕，已接受结果不可被后续尝试替换。
- [x] 数据库能从 Team Report 反查 Snapshot、Finding、Run Capsule、执行策略和具体 Attempt。
- [x] schema 初始化和升级测试可在空库及现有 advisor 库上运行，现有表和数据保持可读。

## 可能触点

- `advisor/db/schema.sql`
- `advisor/db/migrate.py`
- `advisor/research/store.py`
- `advisor/research/artifacts.py`
- `tests/advisor/research/test_artifact_store.py`
- `tests/advisor/research/test_control_plane.py`

## 验证

```bash
./.venv311/bin/python -m pytest tests/advisor/test_db_schema.py tests/advisor/research/test_artifact_store.py tests/advisor/research/test_control_plane.py
```
