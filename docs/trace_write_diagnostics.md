# Trace 写入耗时诊断

本批增加诊断能力，不代表已经修复历史 Trace 超时。生产启用须另行发布。

## 入口与边界

启用 Trace Recorder 的 Agent/Web `/health` 在 `trace_events.write_diagnostics`
下增加有限内存快照。读取诊断本身不访问 SQLite、不调用模型、不重放事件。
HTTP 请求仍按原来的中间件策略产生 Trace，不因查询诊断改变采集策略。
直接调用 Store.write（没有 Recorder）不采集这些指标；诊断不会写入 Trace 表，
不会递归生成证据。未配置 Recorder 的 health 不提供该字段。

CLI 历史库诊断无法还原这些进程内耗时；CLI、飞书及项目检索 Skill 的调用方式不变。
8795 不是数据所有者，也不需要改动或重启。公共回复、业务输出与错误码保持原样。

## 快照含义

- `attempts`：实际尝试写入的次数，含锁竞争后重试；不等于业务请求数或 dropped。
- `retryable_attempts` / `last_retryable`：维护锁或进程内锁未获得的尝试；
  该快照只表明该次尝试可按原规则等待，不证明下一次一定成功。
- `over_budget_attempts` / `last_over_budget`：总耗时超过 500 ms 的尝试，
  包含仍然成功的慢提交；不改变 health 的成功/失败判定。
- `stage_max_ms`：本进程各固定阶段的最大单次尝试累计耗时，单位毫秒。
- `recent_failures`：最多最近 8 次非重试类、非重复终态异常尝试，按时间升序。
  后续成功不清空它们，重启会清空全部诊断；没有自动持久化。

各样本只保留固定事件类型、异常类别白名单、UTC 完成时间、预算与总耗时、
固定阶段耗时和事务进展。不包含 Trace/event/session/identity ID、题目内容、
用户 stage 文本、路径、SQL 或参数、完整异常正文。未知异常类统一为 `unknown`。
计数与耗时上限为 2,147,483,647；阶段键数量固定，最大 16 项。
health 返回独立副本，调用方无法修改内部样本。

## 如何定位

先看 `recent_failures` 的 `error_kind`、`failure_stage`、`slowest_stage`、
`stage_ms` 与 `transaction_state`：

- `path_checks`：路径/元数据检查及父目录准备；多次检查在同次尝试内相加。
- `path_lock` / `store_lock` / `maintenance_lock`：锁获取耗时，不含持锁执行业务耗时。
- `connect`：SQLite 连接及其超时参数准备。
- `schema` / `schema_commit`：表结构、索引、身份校验/初始化及准备提交。
- `begin` / `capacity` / `insert` / `precommit_check`：事件事务获取、容量统计、插入与提交前预算检查。
- `rollback` / `duplicate_check` / `commit` / `close`：回滚、终态去重检查、提交、关闭连接。
- `write`：无法定位到更细阶段的错误，如自定义 Store 或进入写入前就已取消。

`failure_stage` 是最先抛异常的位置，**不一定是耗时来源**。
例如路径检查耗尽预算后，可能到获取锁时才报错；插入变慢可能到
`precommit_check` 才报错。结合 `slowest_stage` 和分段耗时判断。
回滚或关闭再次报错时保留首次失败阶段，`error_kind` 是最终传播的异常类别。
阶段最大值来自不同尝试，不能把它们相加当成某一次总耗时。
诊断使用高精度 perf_counter，避免 Windows 低分辨率时钟把短步骤记成 0/16 ms；
原有预算时钟不变。测量的是实际经过时间，包含线程未获调度时间，不能单凭标签证明是磁盘硬件问题。

`transaction_state` 仅描述该条事件的事务：

| 值 | 含义 |
| --- | --- |
| `unknown` | Store 未提供事务阶段（例如自定义 Store），不推断是否执行过写入 |
| `not_started` | 尚未开始事件事务；不代表 schema 准备从未写库 |
| `begin_unknown` | BEGIN 已尝试但未确认返回 |
| `active` | BEGIN 已正常返回 |
| `commit_unknown` | commit 已尝试但未确认返回，可能已经提交 |
| `committed` | commit 已正常返回，之后 close 仍可能失败 |
| `rollback_unknown` | rollback 已尝试但未确认返回 |
| `rolled_back` | 显式 rollback 已正常返回 |

这些值不是新的执行授权或恢复收据。0.5 秒预算、最多 30 秒锁等待与现有重试逻辑均保持不变；
不自动重试不确定 SQLite/提交结果。预算是协作式检查，不能强制中断 OS I/O。
提交超预算但成功仍计入 written；提交返回未知、关闭失败仍按原行为计失败。
维护等待最终取消/超时属于外层处理，可结合原有 last_failure_kind 与 last_retryable 判断，
不伪造一条新的数据库尝试。两组计数由同一 writer 先后更新，读取时可能短暂相差一条。

## 验证

`tests/test_trace_write_diagnostics.py` 使用独立临时库和受控时钟验证路径/插入超时、
慢提交成功、提交结果未知、回滚/关闭失败、锁重试、重复终态、样本容量与脱敏、
health 无 SQLite I/O 和并发 Recorder 隔离。保留原 Trace 回归覆盖原有业务边界。
