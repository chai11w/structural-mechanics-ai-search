# 阶段五运维：诊断、费用对账与清理

本入口只管理明确指定的阶段五执行库，不发现、停止、重启或发布服务，不调用模型，不读取本地密钥配置。开发验证使用隔离临时数据。生产启用与计划任务安排另行处理。

## 只读诊断和计划

```powershell
python scripts/tiku_execution_maintain.py inspect --database <runtime>/execution.sqlite3
python scripts/tiku_execution_maintain.py cost-plan --database <runtime>/execution.sqlite3 --ledger <runtime>/model_costs.sqlite3 --plan-out <new-plan.json>
python scripts/tiku_execution_maintain.py cleanup-plan --database <runtime>/execution.sqlite3 --compact --plan-out <new-cleanup-plan.json>
```

`inspect` 和两种 plan 使用 SQLite 只读连接与一致读事务，不初始化数据库、推进任务或确认调用。诊断输出操作状态计数、待对账数量、最近操作 ID 及记录数量；不输出身份、原输入、模型正文、账本载荷或绝对私有路径。过期租约在诊断中显示 UNKNOWN，但只有写操作才能持久改变状态。

默认每批最多 20 个主体，可用 `--limit` 调整到 1～100。计划有效期为 15 分钟，绑定数据库、选择项、来源摘要与服务端时间，带 `plan_hash`。计划文件只允许新建，不覆盖已有文件。空费用计划表示当前没有待处理项，不是错误；没有 READY 项时 apply 不创建备份或写账本。

## 费用对账

```powershell
python scripts/tiku_execution_maintain.py cost-apply --database <runtime>/execution.sqlite3 --ledger <runtime>/model_costs.sqlite3 --plan <reviewed-plan.json> --confirm-plan-hash <plan_hash> --backup-dir F:/cc/_backups/7-题库检索/<date>/<new-run>
```

可以用重复的 `--run-id` 限定计划，最多为本批 limit 个。每项列出调用记录数 `calls`、已确认数 `confirmed_calls`、未发送数 `not_sent_calls`、来源摘要和原因：READY、RUNNING、UNKNOWN_USAGE、ALREADY_CONFIRMED、CONFLICT、WRONG_LEDGER 或 MISSING_EVIDENCE。只有 READY 会执行；账本必须匹配调用时记录的目标摘要。已发送调用缺失 usage、结果未确认和冲突不能当作零费用或由操作员盲目标成功。

apply 重查所有选择项。执行库存在有效 RUNNING 租约时返回 EXECUTION_BUSY，避免备份占用模型正在确认结果所需的写事务。符合条件后先用 SQLite backup 保存执行库与既有费用库，并执行 quick_check，再重放原有精确费用写入。outbox 尚未建立时，从确认记录生成确定的 interrupted 载荷。

进程在发送前退出时，调用记录可能仍为 PREPARED。只有原执行者已失权（终态或租约已过期）、记录仍为 PREPARED 且没有响应/usage 时，受审核的 apply 才能将其标记为 NOT_SENT；原 call_id 保留，不虚构模型响应或 token 数。费用载荷只包含 CONFIRMED 调用；全部未发送时账本保留原 run 的零调用记录。混合已确认和未发送调用只记实际确认部分。审核后记录变为 SENT 或其他内容漂移，原计划失效；SENT/UNKNOWN 仍须对账，不能按未发送处理。这一处理不恢复业务执行，也不令 UNKNOWN 操作自动成功。

业务不会重跑。相同 run/call 内容已经落账但 ACK 丢失时，核对原记录后确认即可；相同 ID 内容不一致时不覆盖账本，冲突持久保留。如果账本已提交而执行库事务随后失败，备份 result.json 标明可能已有账本写入，应重新 plan。再次对账仍使用同一调用身份，不能创建替代调用或重新估价。

## 清理与容量回收

```powershell
python scripts/tiku_execution_maintain.py cleanup-apply --database <runtime>/execution.sqlite3 --plan <reviewed-cleanup-plan.json> --confirm-plan-hash <plan_hash> --backup-dir F:/cc/_backups/7-题库检索/<date>/<new-cleanup-run>
```

启用阶段五时持久登记 artifact roots 与 ExecutionPolicy。清理只进入这些根目录下、同时位于该执行库 runtime 内的路径，不清理题库原文件。可以在 plan/apply 两侧使用相同的 `--artifact-root <approved-subtree>` 缩小目录。根目录、父路径和目标文件均拒绝链接/junction 越界；数据库、JSON 配置、日志、key 和 TOML 不作为普通孤儿文件删除。未登记文件只识别图片/PDF 与本项目 `.part-<16位十六进制>` 临时文件。

以下信息会保护文件：有效的当前父子状态、未决操作所在代次的父子状态、未退役操作的结果及文件记录、子题交接收据。UNKNOWN、有效运行、待确认费用或费用冲突不会因普通 TTL 清理失去所需记录。清理失效 RUNNING 时只转 UNKNOWN，不接管或重发。

只有 SUCCEEDED/FAILED/CANCELLED 超过 history_ttl（默认 30 天）、所属代次已退出有效期且费用均确认，才可退役。文件还必须不被保护引用，且修改时间与创建时间均早于保留截止点；新复制的文件不会因为继承题库原文件的旧 mtime 被立即当作孤儿。引用会在数据库写事务内再次核验，文件则核对尺寸、mtime/ctime 与 SHA-256。

清理计划同时绑定完整执行元数据摘要及各文件摘要。状态、来源或文件变化使旧计划失效，必须重新计划。扫描每批最多 10,000 个目录/文件项、摘要文件总字节最多 256 MiB，文件删除数受 limit 限制；扫描超限返回 EXECUTION_MAINTENANCE_SCAN_LIMIT，应选更小子目录。大于单批摘要预算的文件不会自动删除。相关记录按外键次序删除，过期且无剩余历史/操作的 session 才会释放会话容量。

所有文件先复制到仓库外的新备份目录，并核验摘要，之后才开始删除。备份中有执行库、原计划及 `files/0000.bin` 等文件，其序号与计划 files 数组对应。文件删除与 SQLite 提交不能构成跨系统原子事务：若进程中断，PREPARED 状态和备份仍在，必须重新检查路径及数据库、重新 plan；不要直接覆盖后来生成的新文件或把旧数据库覆盖回运行状态。

`--compact` 是清理计划的一部分。元数据提交后尝试 VACUUM 和 WAL truncate，数据库忙或 checkpoint 未完成时报告 RETRY_REQUIRED，已成功清理不会被误报为回滚。重新 plan 后可再尝试。默认不做无限等待或循环清理，不部署周期任务。

备份目录必须新建，位于运行目录及任何 Git 工作区之外；空间不足先拒绝。备份包含受控运行数据，应按既有项目备份权限保管，不加入代码提交。

## 验证范围

`tests.test_execution_maintenance` 验证只读、不输出原文、两库一致备份、CLI 确认摘要、证据漂移、usage 未知、错误账本、过期计划、账本 ACK 丢失、内容冲突及提交后中断重做计划。

`tests.test_execution_retention` 验证过期文件/父子历史/session 清理、备份内容一致、压缩、状态/字节漂移、备份失败零删除、数量上限、越界根目录、保留 UNKNOWN 的原图/子题收据、租约过期分类、费用未确认保护、孤儿清理不动数据库/原始输入、旧 key 不复活及运行中拒绝 apply。当前专项 20 项通过；全阶段的真实浏览器、独立进程中断与发布回退演练仍由 5.5 验收。

该批全仓 **1557 项通过（103.658 秒）**，日志 `.tmp_phase5_1/maintenance_full.txt`。随后收紧 CLI 参数：apply 的选择项完全来自已审阅计划，额外 run-id/limit/compact 被拒绝，不能让操作员误以为它们会缩小或改变 apply 范围；相关 9 项重新通过。没有访问实际供应商或操作生产 runtime。
