# 独立服务身份的状态路径

力答部署复用现有 8790 和 8795 入口，将程序和业务状态分开授权。默认参数及原数据位置保持兼容；新参数仅由维护者固定到服务启动配置，不是模型工具参数，也不自动迁入旧资料。

| 入口 | 可选参数 | 用途 |
| --- | --- | --- |
| 8790 Python / watchdog | `--feedback-database` / `-FeedbackDatabase` | 指向与运营后台共享的反馈 SQLite；附件目录为其同级 `feedback_cases` |
| 8795 Python / watchdog | `--control-db` / `-ControlDb` | 与 8790 共用控制库，将邀请码加密密钥留在仅运营后台可读的 AdminRuntime |
| 8795 Python / watchdog | `--feedback-database` / `-FeedbackDatabase` | 对同一反馈库审核、归档及删除；不因此取得其他搜索会话的写权 |
| 8790 Python / watchdog | `--evidence-data-root` / `-EvidenceDataRoot` | 显式指定程序仓库外的证据数据边界；RuntimeDir 必须是其直接子目录 |

未指定 EvidenceDataRoot 时，仍核对原源码仓库或 linked worktree 的主检出目录。指定时要求已存在的绝对普通目录，拒绝链接、卷根、程序目录重叠和不匹配的运行目录；仍要求完整容量与保留配置。原运行证据计划绑定精确 RuntimeDir 和这个数据边界，备份必须在该边界及源码之外。不会取消原搜题运行证据的容量/保留控制，也不启用新的题库整库备份。

正式迁入需要停止受影响写者后复制控制库、反馈库及其媒体；SQLite WAL/SHM 和运行期重建文件的权限必须跟随各自目录。配置路径不等于数据迁移，不能创建空数据库冒充原来的账户或反馈。8795 仍从指定搜索 SourceRuntime 只读费用和诊断，费用库路径没有新增配置。本阶段暂不接入飞书，保留原服务和数据。

验证：`PYTHONPATH=tests` 下运行 `python -B -X utf8 -m unittest test_service_state_paths test_tiku_agent_8790_a3_v1 test_tiku_agent_8790_retention_config test_tiku_admin test_tiku_agent_watchdog_8790 test_tiku_admin_watchdog_8795 -q`，44 项通过（5.528 秒）。新增两项使用真实 SQLite、邀请码认证及运营 HTTP 审核，确认两项服务操作同一指定反馈记录、不在各自私有目录新建替代控制/反馈库；另核对数据边界和备份越界拒绝。实际 Windows PowerShell 解析两个 watchdog 及参数元数据通过。没有运行模型、飞书或改正式服务；运行账号的应用验收仍需切换时完成。
