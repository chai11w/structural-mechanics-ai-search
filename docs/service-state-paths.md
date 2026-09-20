# 独立服务身份的状态路径

力答部署复用现有 8790 和 8795 入口，将程序和业务状态分开授权。默认参数及原数据位置保持兼容；新参数仅由维护者固定到服务启动配置，不是模型工具参数，也不自动迁入旧资料。

| 入口 | 可选参数 | 用途 |
| --- | --- | --- |
| 8790 Python / watchdog | `--feedback-database` / `-FeedbackDatabase` | 指向与运营后台共享的反馈 SQLite；附件目录为其同级 `feedback_cases` |
| 8795 Python / watchdog | `--control-db` / `-ControlDb` | 与 8790 共用控制库，将邀请码加密密钥留在仅运营后台可读的 AdminRuntime |
| 8795 Python / watchdog | `--feedback-database` / `-FeedbackDatabase` | 对同一反馈库审核、归档及删除；不因此取得其他搜索会话的写权 |
| 诊断 CLI | `--feedback-database`、`--response-database` | 显式指定只读反馈与 Response 数据源；各自省略时仍读 RuntimeRoot 同名库 |
| 8790 Python / watchdog | `--evidence-data-root` / `-EvidenceDataRoot` | 显式指定程序仓库外的证据数据边界；RuntimeDir 必须是其直接子目录 |

未指定 EvidenceDataRoot 时，仍核对原源码仓库或 linked worktree 的主检出目录。指定时要求已存在的绝对普通目录，拒绝链接、卷根、程序目录重叠和不匹配的运行目录；仍要求完整容量与保留配置。原运行证据计划绑定精确 RuntimeDir 和这个数据边界，备份必须在该边界及源码之外。不会取消原搜题运行证据的容量/保留控制，也不启用新的题库整库备份。

正式迁入需要停止受影响写者后复制控制库、反馈库及其媒体；SQLite WAL/SHM 和运行期重建文件的权限必须跟随各自目录。配置路径不等于数据迁移，不能创建空数据库冒充原来的账户或反馈。8795 从指定搜索 SourceRuntime 只读费用、Trace 和任务证据；诊断反馈使用配置好的 FeedbackDatabase，Response 使用该反馈库同目录的 `responses.sqlite3`，与 8790 写入规则一致。显式数据源缺失时报告缺失，不回退读取旧运行目录中的同名历史库。费用库路径没有新增配置。本阶段暂不接入飞书，保留原服务和数据。

8795 的费用总览、反馈关联费用、删除邀请码前的费用记录检查均使用只读查询器。WAL 数据库先复制数据库和 WAL 到自身临时目录，核验稳定快照后再查，避免为了读取而在搜索目录创建或更新 SHM。运营账号需要搜索证据的读取权限及自身临时目录的写权限，不需要搜索会话库、执行库的写权限；共享控制和反馈的既有管理写权限另行保留。已部署环境收紧 ACL 前，必须先发布这一版本，再以真实运营服务账号验证总览、反馈时间/费用及完整 Trace 关联，不直接对旧版撤销写权限。

跨目录诊断示例（路径由维护者指定，不接收用户请求中的任意数据库路径）：

```powershell
python -B scripts/tiku_diagnostics.py --runtime-root <搜索运行目录> --feedback-database <共享目录>/feedback.sqlite3 --response-database <共享目录>/responses.sqlite3 --feedback-id <反馈编号> --association-mode authoritative-only
```

这次修复只更改诊断读取和后台费用读取，不迁移数据库，不修改飞书或检索 Skill 的业务接口。回归覆盖三种 ID 入口的跨目录关联、旧库不回退、CLI 参数、8795 认证后的 Trace API，以及 WAL 费用读取与源文件不变。

2026-09-20 本地修复验证：`python -B -m unittest tests.test_tiku_diagnostics tests.test_tiku_diagnostic_evidence tests.test_tiku_diagnostic_legacy_feedback tests.test_tiku_diagnostic_compare tests.test_tiku_admin tests.test_tiku_admin_trace_api tests.test_tiku_admin_operations_summary tests.test_service_state_paths -q`，66 项通过。用新代码只读查询 2026-09-17 的真实反馈，反馈/Response 入口各返回 2 个 Trace、20 条事件、1 条 Response、1 条反馈、5 次模型调用；对应业务 Trace 入口返回 18 条事件并正确关联 Response 和反馈，时间字段齐全。未重放模型请求。此记录仅证明本地修复与数据核验，尚未切换生产 8795 或修改其 ACL；当前非管理员会话无法读取运营服务进程的完整身份，上线前须在有权限的维护会话中完成核验。

以下为原路径拆分时的历史验证记录：

后续 2026-09-20 已完成 8795 上线及运营账号多余写权限收紧，实际运行版本、验证范围和回退资料见 [8795 发布记录](8795-shared-diagnostics-release.md)。上文“尚未切换”仅描述本地修复时刻。

验证：`PYTHONPATH=tests` 下运行 `python -B -X utf8 -m unittest test_service_state_paths test_tiku_agent_8790_a3_v1 test_tiku_agent_8790_retention_config test_tiku_admin test_tiku_agent_watchdog_8790 test_tiku_admin_watchdog_8795 -q`，44 项通过（5.528 秒）。新增两项使用真实 SQLite、邀请码认证及运营 HTTP 审核，确认两项服务操作同一指定反馈记录、不在各自私有目录新建替代控制/反馈库；另核对数据边界和备份越界拒绝。实际 Windows PowerShell 解析两个 watchdog 及参数元数据通过。没有运行模型、飞书或改正式服务；运行账号的应用验收仍需切换时完成。
