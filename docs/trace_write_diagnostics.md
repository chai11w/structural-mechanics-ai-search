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

## 容量检查修复（2026-09-20，本地验证，尚未发布）

当天 8790 的只读健康快照中，Trace 写失败为 76 次，最近一次发生于北京时间
14:35:43；最近 8 条失败均以 `capacity` 为最慢阶段。该阶段原来在每次事件写入的
事务内执行 `SELECT COUNT(*) FROM trace_events`，随历史数据增长重复扫描索引。
隔离样本复现了扫描及耗时增长，但未复现线上超过 500 ms 的延迟；不能据此断言
磁盘、锁或操作系统调度的具体根因，也不能把本地通过当作线上已经恢复。

修复在同一 SQLite 文件中新增 `trace_event_counts` 单行计数表和事件 INSERT/DELETE
触发器。容量准入仍处于原 `BEGIN IMMEDIATE` 事务，插入、删除和计数一起提交或回滚；
多进程争用最后一个名额不会超限。健康容量快照也读取该精确计数，避免后台重复扫描。
保留原容量上限、500 ms 写预算、维护锁、终态去重和未知提交不重放规则。

旧库首次写初始化在 SQLite 写锁内一次性计数，并原子建立表和触发器；8790 现有
`ensure_store_identity()` 在启动 Recorder 前完成这一步，不把迁移放到正常每条事件的
预算中。迁移保留 store identity、事件内容及事件协议版本。迁移中断或进程退出可重试；
发现部分计数结构、缺失计数行或变更触发器时拒绝写入，不静默重建并掩盖异常。
只读诊断可读取尚未迁移的旧库，此时回退到原全量计数，不触发迁移。

旧版正常 INSERT/DELETE 及原保留维护操作仍受数据库触发器约束，因此旧代码回退后
计数继续更新。回退代码不应删除计数表或触发器；发布前仍须备份并核验固定 release。
本改动不恢复历史丢失事件，不处理后台 marker 的崩溃窗口或 Checkpoint 预算问题。
CLI、网页、飞书及题库检索 Skill 没有参数或结果格式变化，无须修改调用方式。

本机合成数据测量（每种查询 30 次、各用新 SQLite 连接，单位 ms）：

| 事件数 | 全量 COUNT 中位数 / P95 | 维护计数中位数 / P95 | 一次迁移 |
| --- | --- | --- | --- |
| 100,000 | 5.705 / 6.119 | 0.755 / 0.906 | 14.929 |
| 1,000,000 | 44.033 / 66.049 | 0.832 / 1.673 | 64.270 |

两组各经实际 Recorder 写入 30 条，均无写失败或超预算；写事务内 `capacity` 阶段
最大分别为 0.033 / 0.109 ms。P95 使用 nearest-rank 算法。数据为隔离合成样本，
不含用户资料，不是生产延迟承诺。

可复现验证：

```powershell
python -B -m unittest tests.test_trace_capacity tests.test_trace_events tests.test_trace_write_diagnostics tests.test_checkpoint_retention
python -B -m tests.trace_capacity_benchmark
```

回归覆盖旧库只读与迁移、迁移中断/进程退出、跨进程容量竞争、旧写入/删除、重复终态、
超时回滚、清理与重复清理、计数结构损坏，以及初始化后禁止全量 COUNT 的写入/健康路径。
包含 Trace、保留维护、Checkpoint 健康、诊断 CLI、管理 API、后台生命周期及发布的
13 个测试模块共 149 项通过；本次未运行全仓测试，历史题库清理失败不在本补丁范围。
生产是否修复须在单独授权发布后观察新事件写入、容量计数和失败增量，不能以重启归零验收。
