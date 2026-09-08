# 阶段 4.2 Store 与 Retention Runbook

本文件记录阶段 4.2 的控制面验收和 8790 生产启动边界。4.2 只提供
Checkpoint/Artifact 生命周期、容量闸门和 Trace 清理；它不接入 A2/A3
Checkpoint 自动采集，也不改变检索、排序、计费或公共响应。

人工查看、延长和删除入口见 [4.5 runbook](checkpoint_phase4_5_runbook.md)。显式延期可调整
调查/反馈保留类别，但不修改中间结果；其逐目标管理计划与本文件的周期清理计划分别管理。

## 运行目录

- 8790 证据运行根：仓库内 `.tmp_tiku_agent_v2_prod_8790/`
- Checkpoint 数据库：`checkpoint_evidence.sqlite3`
- Artifact 受控根：`checkpoint_artifacts/`
- Trace 数据库：`trace_events.sqlite3`
- retention 备份根：仓库外的 `F:\cc\_backups\7-题库检索\`（实际部署可指定其他仓库外绝对路径）

证据运行根只能位于本仓库内，备份根必须位于仓库外。不得把证据数据库、Artifact、备份、
测试输出或运行日志加入普通代码提交。

## 七项容量

生产入口不提供隐式容量默认值。每次启动和每次 retention 运行都必须显式传入：

```text
max_checkpoint_rows
max_artifact_rows
max_audit_rows
max_trace_rows
max_artifact_bytes
min_free_bytes
max_artifacts_per_checkpoint (1..50)
```

当前可核验的只读基线（2026-09-05）是：8790 Trace 35,475 行、16,750 个完整
`trace_id`、覆盖 2026-08-27 至 2026-09-05，完整日峰值 5,998 行；现有媒体样本
52 个文件，P95 为 365,742 字节，F 盘剩余约 238 GB。Checkpoint/Artifact 生产采集
尚未启用，因此其容量值必须由发布负责人根据受控样本和磁盘预算填写，不能从本文件
臆造。容量值应保留在受控启动参数或外部部署配置中，不写入源码默认值。

## 启动检查

8790 的启动命令必须包含全部容量和 retention 参数。下面用 PowerShell 环境变量表示
部署负责人已经核准的数值：

```powershell
$EvidenceArgs = @(
  "--max-checkpoint-rows", $env:TIKU_MAX_CHECKPOINT_ROWS,
  "--max-artifact-rows", $env:TIKU_MAX_ARTIFACT_ROWS,
  "--max-audit-rows", $env:TIKU_MAX_AUDIT_ROWS,
  "--max-trace-rows", $env:TIKU_MAX_TRACE_ROWS,
  "--max-artifact-bytes", $env:TIKU_MAX_ARTIFACT_BYTES,
  "--min-free-bytes", $env:TIKU_MIN_FREE_BYTES,
  "--max-artifacts-per-checkpoint", $env:TIKU_MAX_ARTIFACTS_PER_CHECKPOINT,
  "--checkpoint-retention-backup-root", "F:\cc\_backups\7-题库检索",
  "--checkpoint-retention-interval-seconds", $env:TIKU_RETENTION_INTERVAL_SECONDS,
  "--checkpoint-retention-backup-keep-runs", $env:TIKU_RETENTION_BACKUP_KEEP_RUNS
)
python -B scripts/run_tiku_agent_8790.py --port 8790 `
  --runtime-dir .tmp_tiku_agent_v2_prod_8790 `
  --control-db .tmp_tiku_admin_8795/control.sqlite3 @EvidenceArgs
```

启动前应验证七项值为正数、`max_artifacts_per_checkpoint` 不超过 50、备份根为仓库外
绝对路径，并确认命令行中没有密钥、邀请码明文或管理员凭据。看门狗和控制切换脚本
使用同一组参数；它们只接受 8790，不得传入 8896 或其他运行根。

### 阶段 4 完整采集发布

使用 `tiku_agent_watchdog_8790.ps1` 发布时，同时传入
`-EnableA2CheckpointCapture -EnableA3CheckpointCapture`。看门狗将经过 manifest 校验的
发布 commit 作为 `--checkpoint-code-revision` 传给 Python；A3 单独开启会拒绝启动。
不传开关仍保持采集关闭。控制库迁移脚本不是完整采集的代码发布入口。

隔离的 linked worktree 发布可以沿用同一 Git 仓库主工作区里的生产 runtime。
启动器通过 Git common directory 校验两者属于同一仓库；不接受无关目录。周期清理也绑定
该主工作区，备份必须同时位于代码 worktree 和主工作区之外。不得为满足目录校验移动生产
会话、复用测试 runtime 或关闭范围校验。

### 已有 Trace 数据库的 identity 迁移

如果 `trace_events.sqlite3` 已存在，8790 启动会先校验完整 Trace V1 schema，并为没有
持久 identity 的完整旧库写入一次 identity；已有 identity 会保持不变。数据库不存在时
不会为了迁移创建空库，首次写入才按正常 writer 流程创建。空库、半成品、损坏文件、符号
链接或其他非普通文件都会 fail-closed，不能被当作“没有候选”的成功结果。迁移完成后再生成
retention plan，使 plan 中的 Trace identity 与实际数据库一致。

## Plan / Apply

独立 CLI 默认只生成脱敏计划；`--apply-plan` 和 `--run-once` 才会执行清理。Apply 必须
使用精确计划哈希、显式允许的 8790 runtime、仓库外备份，并先完成 SQLite 在线备份和
完整性校验。计划绑定 Checkpoint/Trace 数据库的持久 store identity；数据库缺失或被替换
时必须 fail-closed，不能把“候选不存在”误报为幂等成功。

```powershell
python -B scripts/tiku_checkpoint_retention.py --help
python -B scripts/tiku_checkpoint_retention.py --runtime 8790 `
  --backup-keep-runs $env:TIKU_RETENTION_BACKUP_KEEP_RUNS `
  --max-checkpoint-rows $env:TIKU_MAX_CHECKPOINT_ROWS `
  --max-artifact-rows $env:TIKU_MAX_ARTIFACT_ROWS `
  --max-audit-rows $env:TIKU_MAX_AUDIT_ROWS `
  --max-trace-rows $env:TIKU_MAX_TRACE_ROWS `
  --max-artifact-bytes $env:TIKU_MAX_ARTIFACT_BYTES `
  --min-free-bytes $env:TIKU_MIN_FREE_BYTES `
  --max-artifacts-per-checkpoint $env:TIKU_MAX_ARTIFACTS_PER_CHECKPOINT
```

清理按完整 Trace 时间线执行，只删除 cutoff 以前的完整 `trace_id`；新鲜 Trace 不因容量
满而被淘汰。在线 writer 与 retention 使用同一运行锁，维护期间新 Trace 按 fail-open 语义
丢弃并在 health 中保留有界降级计数，避免删除后又追加到旧时间线。Apply 的阶段进度、
成功结果和受控失败均写入备份目录；同一计划可在中断后安全重试，备份 manifest 每次重放
都必须重新校验。

## 验收门

在下列项目全部有测试证据前，保持 A2/A3 自动采集关闭：

1. Store 用可信时钟生成 TTL，过期 Checkpoint/Artifact 读取 fail-closed。
2. 七项写前容量门覆盖并发、写后磁盘余量和“先清过期再重判”。
3. 查看、延期、删除和自动清理审计在容量边界下仍遵守 fail-closed/保留清理槽位。
4. Artifact 先 tombstone，再尝试物理删除；删除失败不得恢复访问，释放字节按实际文件计数。
5. retention plan/apply 覆盖 Checkpoint、Artifact、到期审计、孤儿和批准范围内 Trace，
   并验证同计划重试、部分备份、数据库替换和跨进程锁。
6. `/health` 只输出白名单字段；证据或 Trace 降级时顶层状态为 `degraded`，搜索仍可继续。

阶段 4.2 通过后才可另行启动 4.3/4.4 的 A2/A3 受控采集开发；本阶段不修改任何现有
8790、8795、8788 服务进程。
