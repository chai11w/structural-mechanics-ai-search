# 阶段 4.5 诊断读取与四阶段验收

4.5 提供可信本机操作员使用的 Checkpoint 诊断 CLI，支持从 Trace 定位阶段证据、读取
受权图片、查看前驱链，以及经 plan/confirm/apply 延长或删除证据。四阶段完成指开发和
隔离验收完成；A2/A3 生产采集仍默认关闭，部署和启用沿用 [4.4 runbook](checkpoint_phase4_4_runbook.md)。

## 入口与权限

- `scripts/tiku_diagnostics.py` 保持只读，只从 Trace 白名单属性投影格式合法的
  `checkpoint_id`，不导出私有 session key、Checkpoint 内容或图片。
- `scripts/tiku_checkpoint_diagnostics.py` 使用真实 Store。查询和查看会写证据审计，
  必须使用可写数据库；审计无法提交时不返回证据。数据库不存在时拒绝且不创建空库。
- 该 CLI 依赖本机操作员的文件系统权限。`actor_key` 是审计身份，`identity_key` 和
  scope 是访问边界，不构成独立登录认证；本次没有新增网页、远程 API 或 8795 角色授权。
- 每次调用都显式提供七项容量，与对应 runtime 的受控配置一致。运行根指向该实例的
  `checkpoint_evidence.sqlite3`、`checkpoint_artifacts/` 和 `trace_events.sqlite3` 所在目录。
- 仅 `query --trace-id` 允许在指定 `identity_key` 后省略 session，用于受审计的 session
  发现。随后所有具体读取和管理都要求返回的完整 session 哈希。按 workflow 查询还必须
  同时给出 `workflow_search_id/workflow_task_revision`，不能只给其中一项。

## 查询和查看

下列环境变量由操作员根据实际实例填写，容量不能照抄测试值。命令从目标 checkout 执行，
全局参数放在子命令之前。身份为内部安全 ID，不能填邀请码明文或其他凭据。

```powershell
$DiagnosticArgs = @(
  "--runtime-root", $env:TIKU_EVIDENCE_RUNTIME_ROOT,
  "--actor-key", $env:TIKU_DIAGNOSTIC_ACTOR_KEY,
  "--identity-key", $env:TIKU_DIAGNOSTIC_IDENTITY_KEY,
  "--max-checkpoint-rows", $env:TIKU_MAX_CHECKPOINT_ROWS,
  "--max-artifact-rows", $env:TIKU_MAX_ARTIFACT_ROWS,
  "--max-audit-rows", $env:TIKU_MAX_AUDIT_ROWS,
  "--max-trace-rows", $env:TIKU_MAX_TRACE_ROWS,
  "--max-artifact-bytes", $env:TIKU_MAX_ARTIFACT_BYTES,
  "--min-free-bytes", $env:TIKU_MIN_FREE_BYTES,
  "--max-artifacts-per-checkpoint", $env:TIKU_MAX_ARTIFACTS_PER_CHECKPOINT
)
python -B scripts/tiku_diagnostics.py --runtime-root $env:TIKU_EVIDENCE_RUNTIME_ROOT `
  --trace-id $env:TIKU_DIAGNOSTIC_TRACE_ID --format json
python -B scripts/tiku_checkpoint_diagnostics.py @DiagnosticArgs query `
  --trace-id $env:TIKU_DIAGNOSTIC_TRACE_ID --limit 20
```

查询只返回有界摘要和 owner。即使 Trace 关联写入丢失，仍可按 Checkpoint 自带的
`trace_id` 查到证据。采集关闭、阶段未运行或证据过期时可以返回空集，不补造阶段。
分页使用 `next_checkpoint_id` 作为下一次相同查询的 `--after-checkpoint-id`；每页最多
100 条，游标过期、删除或不在当前 scope 时拒绝。整页查看审计在同一事务提交。

将查询所得的 session、Checkpoint 和 Artifact 内部 ID 分别填入下面使用的环境变量：

```powershell
$ScopedArgs = $DiagnosticArgs + @("--session-key", $env:TIKU_DIAGNOSTIC_SESSION_KEY)
python -B scripts/tiku_checkpoint_diagnostics.py @ScopedArgs show `
  --checkpoint-id $env:TIKU_DIAGNOSTIC_CHECKPOINT_ID
python -B scripts/tiku_checkpoint_diagnostics.py @ScopedArgs chain `
  --checkpoint-id $env:TIKU_DIAGNOSTIC_CHECKPOINT_ID --limit 20
python -B scripts/tiku_checkpoint_diagnostics.py @ScopedArgs artifact `
  --checkpoint-id $env:TIKU_DIAGNOSTIC_CHECKPOINT_ID `
  --artifact-id $env:TIKU_DIAGNOSTIC_ARTIFACT_ID
```

`show` 返回通过契约、完整性、scope、TTL 和审计检查的完整结构化证据。`chain` 沿前驱
向前最多读取 100 条，限制同一 workflow revision，并拒绝跨 unit 或子任务版本；返回的
`stop_reason` 为 `complete`、`limit` 或 `unavailable_predecessor`。缺失前驱不触发业务重跑。

Artifact 必须通过引用它的 Checkpoint 访问，单独持有 Artifact ID 不授予读取权。
默认只输出 descriptor；显式加 `--include-content` 才输出 base64 图片，始终先校验实际
图片和提交审计。CLI 不创建预览或导出文件；将输出另存后，副本不再受 Store TTL 管理。

## 延长和删除

`plan-extend` 和 `plan-delete` 只读取目标并记录查看审计，不修改证据。计划绑定操作员、
scope、runtime 路径摘要、Store ID、七项容量、目标指纹、操作、稳定原因码和有限期限。
计划有效期 15 分钟，备份后和真正进入修改事务时再次检查期限；目标或数据库变化即拒绝。

延长必须提供带时区的新到期时间，且晚于现有期限。可显式将保留类别改为
`investigation` 或 `feedback`，上限分别是从原创建时刻起 90 天和 365 天；不重置创建时间，
不修改阶段结果、排名、owner、输入指纹或图片字节，不支持永久 hold 或复活过期证据。
普通 Checkpoint 的 30 天和图片的 3/7 天窗口已用满时，不能在原类别内再延长。
Checkpoint 和 Artifact 分别管理，延长其中一个不会延长另一个；需要长期查看图片时，
应分别确认引用它的 Checkpoint 和目标 Artifact 期限。

示例计划保存到预先建立的仓库外备份目录，文件不可覆盖：

```powershell
$ManagementBackupRoot = "F:\cc\_backups\7-题库检索\2026-09-07"
$ManagementPlanFile = Join-Path $ManagementBackupRoot "checkpoint-management-plan.json"
python -B scripts/tiku_checkpoint_diagnostics.py @ScopedArgs plan-extend `
  --checkpoint-id $env:TIKU_DIAGNOSTIC_CHECKPOINT_ID `
  --retention-class investigation --new-expires-at $env:TIKU_EVIDENCE_NEW_EXPIRES_AT `
  --reason-code INVESTIGATION_OPENED --plan-out $ManagementPlanFile
```

删除使用 `plan-delete --checkpoint-id ... --reason-code USER_REQUESTED --plan-out ...`；
针对图片时再加 `--artifact-id`。删除 Checkpoint 只删除该阶段及引用关系；删除 Artifact
使其所有引用不可访问，物理 blob 仅在无其他有效 descriptor 使用时释放。

审阅计划后，将确认的完整 `plan_hash` 填入环境变量，再执行：

```powershell
python -B scripts/tiku_checkpoint_diagnostics.py @ScopedArgs apply `
  --plan-file $ManagementPlanFile --confirm-plan-hash $env:TIKU_EVIDENCE_CONFIRMED_PLAN_HASH `
  --backup-root $ManagementBackupRoot --max-backup-runs $env:TIKU_MANAGEMENT_MAX_BACKUP_RUNS
```

## 备份和失败边界

- apply 复用 retention 的跨进程维护锁，先在线备份 SQLite，校验 Store/目标指纹，再备份
  目标 Checkpoint 关联的可用图片或指定 Artifact，写文件哈希、manifest 和计划。
  路径为 `<backup-root>/checkpoint-management/<plan-hash>/`，拒绝链接和仓库/runtime 内备份。
- SQLite 是整库快照，图片只备份本次目标范围；因此它是管理操作回退材料，不能作为
  整个 runtime 的完整恢复包。关联图片已过期或缺失时记录 `unavailable_artifact_ids`；
  已损坏、权限不符、审计失败或哈希不一致时拒绝修改。
- `max_backup_runs` 必须显式设置为 1～10000，统计该桶中全部条目，包括不完整目录；
  达限拒绝新操作，不自动轮转或删除备份。备份前逐项检查磁盘余量，备份副本不受 Store TTL
  自动清理，操作员须另行管理有限保留。4.2 周期清理备份使用独立目录和轮转策略。
- 修改与对应审计在同一事务提交。备份、范围、计划、目标漂移或审计校验失败均拒绝修改；
  错误输出仅含稳定 `failure_code`，不含异常正文或路径。
- 图片 tombstone 和审计已提交但物理删除失败时，返回 `status=applied` 及
  `physical_cleanup_pending=true`；图片保持不可访问，后续 retention 处理遗留 blob。
- 结果回执写入失败不撤销已提交操作，返回 `status=applied`、`receipt_saved=false` 和
  `EVIDENCE_RECEIPT_UNAVAILABLE`。应依据数据库审计和目标状态核对，不盲目重放。
- 已执行、目标变化或已有备份目录的同一计划不会再次执行。部分备份失败后保留现场，
  检查容量和目标状态，再生成新计划；本入口不提供 4.2 自动清理计划的中断续跑功能。

## 四阶段验收

验收使用临时数据库、临时图片、实际 A2/A3 Runtime 和确定性模型替身，不调用外部模型，
不读取 live 题库，不启动服务。各阶段形成以下验证链：

| 验收面 | 覆盖 |
| --- | --- |
| 契约与隐私 | 4.1 白名单、截断、脱敏和稳定失败码；4.5 只读 Trace 投影不导出私有 session |
| 生命周期 | 4.2 可信 TTL、七项容量、审计余量、周期清理与回退；4.5 有限类别变更及过期计划拒绝 |
| 实际阶段 | A3 答案 Trace 可追溯九阶段和受权裁图；standalone A2 可追溯实际六阶段 |
| 行为等价 | A3 采集关闭、开启及容量压力下，公共文本、协议、图片哈希和候选排名一致；沿用 A2 等价测试 |
| 读取与管理 | 跨身份/版本隔离、查询分页、审计事务回滚、图片 TTL/完整性、备份校验、漂移和提交后失败 |

```powershell
python -B -m unittest -q tests.test_checkpoint_diagnostics tests.test_checkpoint_phase4_acceptance tests.test_checkpoint_store tests.test_checkpoint_retention tests.test_tiku_diagnostics tests.test_tiku_diagnostic_compare
python -B -m unittest discover -s tests -p 'test_*.py'
python -B scripts/tiku_checkpoint_diagnostics.py --help
python -B scripts/tiku_diagnostics.py --help
python -B scripts/tiku_checkpoint_retention.py --help
python -B scripts/run_tiku_agent_8790.py --help
python -B scripts/search_by_loads.py --help
python -B search.py --help
```

2026-09-07 验证：新增诊断和四阶段验收 29 项、联合回归 72 项通过；全仓 1412 项通过
（68.504 秒）。六个 CLI 帮助命令及 `git diff --check` 通过。本次只提交本地开发结果，
未推送、部署、重启服务或操作 live 题库。
