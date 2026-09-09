# 5.2 数据契约与离线迁移

状态：数据层及现有 A2/A3 适配验证完成；执行门已由 [5.3](phase5_3_execution_contract.md) 接入并在隔离库验证。默认生产 launcher 不变。

## 权威与兼容

`ExecutionStore` 的一个 SQLite 文件同时拥有 session epoch、当前父/子状态及任务历史。`ExecutionSessionStore(kind='workflow'/'child')` 替代旧 store 接口；启用后不读取或双写旧 session 库。所有写事务使用 `BEGIN IMMEDIATE` 和外键检查。每个状态槽位保留版本墓碑，清除不归零；每次写入检查读取时的 epoch/version，两个独立连接也不能覆盖旧版本。

状态版本附在 Python 对象的私有 `_execution_version` 上，不改变 `AgentState.to_dict()`、父子 `task_revision`、候选 generation 或 V1 公共字段。运行时的可写状态复制显式继承版本。未绑定版本的新对象只能写入确认不存在的槽位，不能盲目覆盖已存在状态。

`execution_sessions` 用随机 epoch 区分重置前后，即使 task ID、unit、旧 revision 重复也拒绝旧写。总 state_version 在任一父/子状态变更时递增。`execution_tasks` 按 epoch/kind/task ID/task revision 保存历史身份、最后 phase 和 state_version；不会把不同尝试覆盖成一条。它是必要任务历史，不是完整中间快照 archive。

子记录的 parent_id 是实际数据库外键，绑定同 session/epoch 的父任务记录及 unit。A3 的 unit 必须属于父任务，子输入与父裁图须存在且内容哈希相等；裁剪 bounds 参与 input_version。direct A2 wrapper 绑定父原图，standalone A2 可无父。已存在 child revision 的父/unit/input_version 不允许改绑；新一次 start_search 是新任务尝试版本。

## 寿命与容量

策略由 `ExecutionPolicy` 校验，全部数字必须为正整数：

| 项目 | 默认值与规则 |
| --- | --- |
| session 活跃期 | 写入续期 2 小时；epoch 绝对最长 30 天 |
| 过期/重置 | 新 epoch 原子清空两个当前状态槽位；旧 epoch 不能再次授权 |
| 任务历史 | 30 天保留目标；活跃/待对账关联禁止普通清理，维护实现接入 5.3/5.5 |
| 会话/任务/操作行数上限 | 10,000 / 100,000 / 100,000；拒绝发生在新增之前 |
| 单状态/单结果/结果总字节 | 1 MiB / 256 KiB / 32 MiB；结果门接入 5.3 |
| DB+WAL / 最小磁盘余量 | 256 MiB / 256 MiB |
| 执行租约/单次最长执行 | 300 秒 / 1800 秒，5.3 已接入；到期不能推断外部请求未执行 |
| 时钟 | 服务端时间持久高水位；小幅回拨使用高水位，超过 300 秒 fail-closed |

旧 epoch 已失效是安全清理操作收据的前置条件；活跃 epoch 的相同键不能在清理后复活。容量数值属于可配置隔离基线，不是生产压测结论。

## 迁移与回退

迁移工具是 `scripts/tiku_execution_migrate.py`，只操作停写的离线 runtime 克隆，不对运行服务做自动发现/停机。先复制完整运行目录并保持其内部图片引用指向克隆，再 plan → 审阅 → apply。工具检查已知原图/裁图/答案路径存在并位于该克隆，拒绝留下 live 路径。

```powershell
python scripts/tiku_execution_migrate.py plan --child-db <clone>/a2/session.db --workflow-db <clone>/a3_sessions.sqlite3
python scripts/tiku_execution_migrate.py apply --child-db <clone>/a2/session.db --workflow-db <clone>/a3_sessions.sqlite3 --destination <clone>/execution.sqlite3 --offline-clone-root <clone> --expected-plan <reviewed-plan.json> --backup-dir <repository-external-new-backup-directory>
```

plan 只输出源数据摘要和行数，不输出会话内容。apply 要求新目标、新备份目录，分别以 SQLite backup 保存源库，核对源和备份均与审阅摘要一致。过期状态不导入；有效旧状态标 `legacy_snapshot`，不制造历史 operation/call 收据。父子/输入不一致的旧数据拒绝迁移，不猜测绑定。导入在同目录临时库完成，先标记 `migration=incomplete`，全部导入及 SQLite 完整性/外键校验通过后才标记 complete、同步并独占发布目标名称。普通失败清除临时库；进程直接退出可能留下临时库，未完成库运行时打开即拒绝，正式目标不出现。发布时目标已被其他写入者创建则拒绝覆盖。

原库始终不变。尚未启用/没有新执行时，回退为放弃新库并使用核验过的源备份。启用后发生的新操作不能靠直接切旧库“回退”；必须先停止新写、保留新库并对账，其费用/副作用收尾属于 5.4 与发布流程。关闭开关不是已发生操作的撤销。

真实 CLI 的隔离迁移、补账、清理和备份恢复证据见 [发布演练](phase5_release_rehearsal.md)。该演练不修改生产运行库、不切换服务。

## 验证

`tests.test_execution_state` 覆盖独立连接旧写、版本不改义、reset ABA、clear 后旧写、未绑定覆盖、可写克隆版本、父子输入/改绑、重试历史、重启、TTL/时钟、容量、迁移源/备份一致性、拒绝变化计划和 live 引用、未完成迁移拒绝，以及现有 A2→A3→裁剪→A2 业务路径使用新 store。

本批执行 `python -B -m unittest -q tests.test_execution_state tests.test_tiku_agent_session_runtime`：50 项通过。费用落账恢复、自动维护和后台调度不由本批声称完成。
