# 8790 自动清理告警诊断

2026-09-28，只读核对生产状态，在独立副本上执行复现。生产版本为 `33f82e474b5884c30c4825687e203636deb8ab19`。本次未修改运行配置、重启服务或手工执行在线清理。

结论：本次 `RETENTION_DRIFT_DETECTED` 的直接原因是 **30 秒维护预算耗尽后被错误归类为数据漂移**。现有工作量超过预算，同时校验有重复扫描和逐条开库的问题；不能仅根据这个错误码推断在线数据发生变化。

## 生产证据

最近三个保留的失败计划均为 `prepared`，完成阶段为空。它们生成最后一版备份清单时，距离任务开始已经过去 44.0、47.2、44.2 秒，之后还会再验证完整备份。后台入口和 `CheckpointRetentionRunner.run_once()` 都设置 30 秒预算。备份结束后必经 `_assert_plan_store_identities()`；此时读取数据库身份会先检查已耗尽的预算。

`_assert_store_identity()` 捕获所有 `Exception`，将 `EvidenceDeadlineExceeded` 转成 `RETENTION_DRIFT_DETECTED`。另一处 `_trace_candidates_already_satisfied()` 捕获 `OSError`；`EvidenceDeadlineExceeded` 继承 `TimeoutError`，后者又属于 `OSError`，同样被误归类。运行器原本区分的 `RETENTION_BUDGET_EXHAUSTED` 因此收不到原始超时异常。

截至本次检查，在线服务累计 7 次清理失败，Trace 计数为 8 次写入失败、8 次丢弃，最近错误为 `TraceEventMaintenanceBusy`。清理持有维护锁期间，日志写入最多等待 30 秒，超时会丢弃相应诊断事件。检查时维护锁已释放，日志持续写入；诊断快照累计存储 74 条、失败 0 条。健康状态仍为 degraded，不只是显示上的告警。

## 隔离验证

工作资料保存在仓库外：`F:\cc\_backups\7-题库检索\2026-09-28\retention-investigation`。保留了 19:28 失败时的计划、SQLite 备份及清单；复现只操作该目录中的副本。

- 8 个候选文件记录与冻结数据库的状态、到期时间、存储键、大小和摘要全部相符。
- Checkpoint 与 Trace 数据库身份均与计划相符。4,516 个 Trace 候选、9,694 条候选事件的完整快照与冻结计划一致。
- 在身份和候选均一致的副本上运行原版清理，30.031 秒后失败。捕获异常链为 `CheckpointRetentionError(RETENTION_DRIFT_DETECTED)` ← `EvidenceDeadlineExceeded`。副本中的删除已经执行，最后的 Trace 复核超时；这一阶段不同于线上备份后即超时，但证明同一错误分类问题及另一个耗时瓶颈。
- 单独给无漂移的数据库身份检查设置已耗尽的预算，稳定得到 `checkpoint store identity is unavailable` / `RETENTION_DRIFT_DETECTED`，底层原因为 `EvidenceDeadlineExceeded`。这与线上备份耗时超过 30 秒后必经的路径一致。
- 对副本已删除的 Trace 做最终复核，10 秒只完成约 655 次逐条查询，总计需要检查 4,516 个候选。每次 `events_for_trace()` 都重新开只读连接并验证结构，这一流程本身也容易超过总预算。

## 修复方向

1. 保留超时异常类别，明确返回维护超时，不把超时、锁等待或 I/O 失败统称为数据漂移。
2. 为 SQLite 完整性检查加协作式截止检查，并减少重复验证。同一备份当前在复制完成、生成清单记录、最终清单验证时会重复执行完整性检查；这些检查目前没有接入维护预算。
3. 用同一个只读事务批量复核指定 Trace 候选，保持数据库身份和候选边界校验，避免逐条开库。
4. 用生产规模的隔离副本验证完整维护周期及日志等待时间，再决定是否需要分批执行。单纯延长 30 秒会延长维护锁占用，不能解决日志写入最多等待 30 秒的问题。

本次完成诊断，尚未修改或发布上述修复。题库、任务、模型效果和费用数据没有执行手工清理；在线失败计划没有进入已完成的删除阶段。隔离副本的清理结果不能当作生产清理已完成。
