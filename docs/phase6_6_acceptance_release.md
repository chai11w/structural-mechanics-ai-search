# 阶段 6.6：统一验收与独立发布

2026-09-13。发布目标为独立 **8898**，保留现有 8790、8896 与飞书服务。开发分支 `codex/phase6-background-execution`；上线版本由外置 manifest 固定，开发 worktree 不直接承载服务。本页记录验收范围，发布完成记录在下方追加。

## F01～F18 证据索引

各门由以下可重复测试共同验证；全量回归包含这些模块及阶段 1～5 的旧行为断言。模块路径均位于 `tests/`，早期浏览器记录与限制见 [6.4](phase6_4_web_recovery.md)，进程死亡与恢复范围见 [6.5](phase6_5_lifecycle.md)。

| 门 | 自动化证据 | 验证结果的含义 |
| --- | --- | --- |
| F01 | `test_background_admission`、`test_execution_dispatch` 的原子接收/事务失败 | 半张上传不接收；ACK 前断线仍按原键发现唯一完整任务 |
| F02 | `test_execution_dispatch` 的重复提交/竞争；`test_background_processes` 的双进程领取 | 相同键不重发，异体冲突拒绝，显式新键按容量准入；唯一生效 attempt |
| F03 | `test_background_stream` 的 queued disconnect；dispatch 队列截止测试 | 关闭真实 ASGI 消费链后仍按原截止失败，零额外调用，不靠观察续期 |
| F04 | `test_background_stream` 的 running disconnect/header disconnect | 所有订阅关闭后后台完成，原调用数不变 |
| F05 | `test_execution_dispatch_a3`、`test_background_http_a3` | 五类 A3 操作、unit/revision、父子与人工裁剪绑定正确 |
| F06 | `test_background_publication`、`test_background_processes` 的 committed crash | 只修复交付；原业务、费用与 Response 不重复 |
| F07 | `test_background_stream` 慢消费者/断档、`test_background_http` read-only queries | 订阅容量有界、断档给最新快照；读取不启动业务 |
| F08 | `test_background_frontend`、6.4 浏览器矩阵、6.6 恢复浏览器复验 | 原键刷新、双标签、旧候选拒绝、Web Lock 缺失时关闭动作入口 |
| F09 | `test_background_http`、`test_background_http_lifecycle`、dispatch 私有输入测试 | 换身份、会话、epoch、失效 Cookie 和猜 ID 均不越权；公共响应不含私有输入 |
| F10 | `test_background_http_lifecycle`、`test_background_lifecycle`、reset 浏览器 | queued/running 撤权与到期阻止新调用/晚写，已发送费用保留 |
| F11 | `test_execution_dispatch`、`test_execution_cost_admission`、lifecycle 授权失败测试 | 接收、领取、prepare/send、结果写入各门重验；待对账阻止新费用 |
| F12 | `test_background_processes` restart/input tests | 未领取任务从持久输入恢复；期限不延长，损坏/失效/版本变化拒绝 |
| F13 | `test_background_processes` effect crash windows、费用对账测试 | PREPARED/SENT/CONFIRMED 分开裁决；UNKNOWN 不自动再发 |
| F14 | `test_background_a3_recovery`、`test_background_http_a3`、恢复浏览器 | 只恢复已有子/逐题收据；控制 ACK 无第二个可评分回复 |
| F15 | dispatch 容量/磁盘测试、`test_execution_retention`、`test_background_lifecycle` | 无虚假接收，旧清理计划失效，未决输入/费用/媒体证据保留 |
| F16 | `test_background_stream` Trace 终态、HTTP 评分/权限测试 | 执行与观察分别唯一终态；断线不污染业务，反馈绑定原 Response |
| F17 | lifecycle drain/schema/policy/禁用测试、真实 crash/竞争进程、发布检查 | 有界关闭；不兼容状态拒绝；看门狗核验完整进程身份，不自动杀死存活的慢调用 |
| F18 | `test_phase6_equivalence`、`test_phase6_launcher`、固定 release smoke | 同一 A2/A3 内核的新旧调度结果、章节、排名、答案及费用等价；独立认证、存储、重启与发布核验 |

`test_phase6_equivalence` 使用真实 A3/A2 状态机、SQLite、图片和可数合成 provider，分别执行手动裁剪及两题自动准备，再完成章节与答案选择。只排除不同操作必然不同的 request/search ID；并行 unit 的完成次序允许不同，调用/费用按多重集合比较。明确断言最终 `4力法`、rank 1、实际合成答案路径，避免两个空结果也通过。

## 本轮验证与限制

- 定向新测试：5 项通过，包括两个完整新旧流程对照和三个生产组件启动/隔离测试。
- 真实 Chromium 恢复复验：9 项通过；page/verifier/child 共 3 次调用，丢恢复 ACK、刷新、双标签及两次反馈均无新增调用。日志 `.tmp_tests/phase6_6/browser-recovery.log`。
- 全量回归：**1,725 tests / OK，253.306 秒，无跳过**；发布 smoke 尚待完成。日志 `.tmp_tests/phase6_6/full-regression.log`。
- 本轮新增付费模型样本 **0**。已有真实模型样本仍沿用前阶段记录；合成矩阵证明执行、持久化、授权与交付语义，不声称证明供应商恰好一次、低清/旋转识别精度或所有真实题目效果。
- 阶段 7 暂停/继续、跨设备历史中心及公网隧道配置不在本次范围。8898 只监听 `127.0.0.1`。

## 发布布局与认证

- 启动入口 `scripts/run_tiku_agent_phase6.py`，8898 独立 Cookie；必须显式指定 runtime。后台模式固定开启，拒绝 8790/8896/飞书端口与 8790/飞书 runtime；存在旧会话需先离线迁移，不自动导入。
- 复用生产 A3/A2 与 checkpoint wiring，保持 1 运行、2 排队、55 秒截止及既有排名策略。独立 Trace/Response/feedback/checkpoint/cost/control 全部置于 runtime。
- 新 runtime：`F:\cc\7-题库检索\.tmp_tiku_agent_phase6_8898`。ACL 仅当前用户、SYSTEM、Administrators。不迁移 8790/8896 的会话、费用、媒体或运行状态。
- 用户将端口选择改为新服务，并将认证方案交由助手决定。本次只从 8795 控制库只读复制邀请码 ID、哈希、状态、版本、到期及单码额度等认证记录；33 条；默认/全站预算和反馈保留设置同步初始化，费用从新服务单独开始。不复制明文码、加密发放库、管理员密码或原 Cookie 密钥；新库生成独立 Cookie 密钥。使用已有邀请码登录，但之后两服务的禁用/撤销/额度分别管理，复制后的变更不自动同步。
- 固定 release：`F:\cc\_deploy\7-题库检索\8898\<commit>`；manifest 与备份在 `F:\cc\_backups\7-题库检索\2026-09-13\phase6-8898`。不复制含密钥配置到代码提交；部署配置依赖需单独核验。
- `tiku_agent_watchdog_phase6.ps1 -ManifestPath <manifest> -CheckOnly` 验证固定版本、干净源码、端口和 runtime。正常运行用互斥锁防重实例；已有监听者须完整身份匹配，陌生进程绝不停止或接管。健康失败但进程仍存活时保留现场，防止长调用被自动杀死；进程确实退出后才启动恢复。

## 回退与维护

1. 先关闭 8898 的新接收并等待执行结束；有 UNKNOWN/费用待对账则保留执行库和原始证据，按维护 CLI 的 plan → confirm → backup → execute 对账，不能自动重发。
2. 停止本次 8898 计划任务/看门狗前重新核验 task action、固定 release、Python、参数、PID 和监听归属；不按端口批量杀进程。保留其他三个服务。
3. 新服务尚无业务写入时可撤销新启动项并保留其目录；有新业务后只能使用兼容当前状态的修复版本。不得把后台标记关掉、切回旧会话库或覆盖旧备份来伪装回退。
4. 停稳后为 execution/control/cost/Trace/Response/checkpoint/feedback 与媒体做一致性备份，备份权限同 runtime；恢复实验继续使用隔离副本。密码/邀请码秘密不进入文档或日志。
5. 不自动推送或合并主线。正式数据和本地配置不进入代码提交。
