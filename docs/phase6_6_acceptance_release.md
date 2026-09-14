# 阶段 6.6：统一验收与独立发布

2026-09-13。发布目标为独立 **8898**，保留现有 8790、8896 与飞书服务。开发分支 `codex/phase6-background-execution`；上线版本由外置 manifest 固定，开发 worktree 不直接承载服务。6.1～6.6 已实现并独立发布；本页记录验收范围、固定版本及回退证据。

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
- 首轮全量：1,725 项通过（253.306 秒）；首页跳转修正后的全量仍为 1,725 项通过（232.918 秒）。数字 ID 诊断修正后的最终全量 **1,726 tests / OK，255.436 秒，无跳过**，日志 `.tmp_tests/phase6_6/full-regression-release.log`。
- 本轮新增付费模型样本 **0**。已有真实模型样本仍沿用前阶段记录；合成矩阵证明执行、持久化、授权与交付语义，不声称证明供应商恰好一次、低清/旋转识别精度或所有真实题目效果。
- 阶段 7 暂停/继续、跨设备历史中心及公网隧道配置不在本次范围。8898 只监听 `127.0.0.1`。

## 发布布局与认证

- 启动入口 `scripts/run_tiku_agent_phase6.py`，8898 独立 Cookie；必须显式指定 runtime。后台模式固定开启，拒绝 8790/8896/飞书端口与 8790/飞书 runtime；存在旧会话需先离线迁移，不自动导入。
- 复用生产 A3/A2 与 checkpoint wiring，保持 1 运行、2 排队、55 秒截止及既有排名策略。独立 Trace/Response/feedback/checkpoint/cost/control 全部置于 runtime。
- 新 runtime：`F:\cc\7-题库检索\.tmp_tiku_agent_phase6_8898`。ACL 仅当前用户、SYSTEM、Administrators。不迁移 8790/8896 的会话、费用、媒体或运行状态。
- 用户将端口选择改为新服务，并将认证方案交由助手决定。本次只从 8795 控制库只读复制邀请码 ID、哈希、状态、版本、到期及单码额度等认证记录；33 条；默认/全站预算和反馈保留设置同步初始化，费用从新服务单独开始。不复制明文码、加密发放库、管理员密码或原 Cookie 密钥；新库生成独立 Cookie 密钥。使用已有邀请码登录，但之后两服务的禁用/撤销/额度分别管理，复制后的变更不自动同步。
- 固定 release：`F:\cc\_deploy\7-题库检索\8898\<commit>`；manifest 与备份在 `F:\cc\_backups\7-题库检索\2026-09-13\phase6-8898`。不复制含密钥配置到代码提交；发布配置只保存已核验的题库根、top_k 和独立答案输出目录；模型凭据来自已有用户环境变量，两种供应商的持久用户变量均已确认存在。未复制配置中的 API key。
- `tiku_agent_watchdog_phase6.ps1 -ManifestPath <manifest> -CheckOnly` 验证固定版本、干净源码、端口和 runtime。正常运行用互斥锁防重实例；已有监听者须完整身份匹配，陌生进程绝不停止或接管。健康失败但进程仍存活时保留现场，防止长调用被自动杀死；进程确实退出后才启动恢复。

## 回退与维护

1. 先关闭 8898 的新接收并等待执行结束；有 UNKNOWN/费用待对账则保留执行库和原始证据，按维护 CLI 的 plan → confirm → backup → execute 对账，不能自动重发。
2. 停止本次 8898 计划任务/看门狗前重新核验 task action、固定 release、Python、参数、PID 和监听归属；不按端口批量杀进程。保留其他三个服务。
3. 新服务尚无业务写入时可撤销新启动项并保留其目录；有新业务后只能使用兼容当前状态的修复版本。不得把后台标记关掉、切回旧会话库或覆盖旧备份来伪装回退。
4. 停稳后为 execution/control/cost/Trace/Response/checkpoint/feedback 与媒体做一致性备份，备份权限同 runtime；恢复实验继续使用隔离副本。密码/邀请码秘密不进入文档或日志。
5. 不自动推送或合并主线。正式数据和本地配置不进入代码提交。


## 8898 发布记录

- 首批修正版 **`292537f0ff99c58a80b15c7674afb412f3740442`**，入口 `http://127.0.0.1:8898/`。开发文档后续提交不要求移动此已验证代码版本。
- 首次候选的首页 401 问题已修正为跳转 `/invite`，API 仍保持 401。另修复数字开头 operation ID 不符合 Trace `operation` 符号约束的问题：提交及流观察使用 `op_<id>` 引用，不放宽校验。新增确定性数字 ID 回归要求零诊断拒绝；合成测试 provider 的 call_type 同步改为合规的下划线命名。原候选已丢失的诊断事件没有伪造补回，旧日志保存在离线快照中。
- 已通过真实 HTTP 登录、会话绑定、提交、SUCCEEDED/READY、原键三次读取、流订阅、旧协议拒绝与邀请码禁用检查。发布探针总计 3 个无图片提示任务、0 次模型调用、0 个费用效果；三个临时验收邀请码均已禁用。用户邀请码保持导入状态。
- 已实际演练平滑停机：先停新服务看门狗，核验 Python 全部参数及独立 console 中只有目标进程后发送 SIGBREAK。Uvicorn 记录 `Application shutdown complete` 后做完整新 runtime 离线备份，五个 SQLite 文件的 quick_check/foreign_key_check 全通过。没有对 8790/8896/飞书发停止信号。
- 版本切换后，验收进程在内存中保留的原登录仍有效；原键读取三次得到同一份完整 publication、同一 Response、原 attempt 仍为 1，模型效果仍为 0。证据 `release-restart-smoke.json`。
- 最终 smoke：健康 `ok`、Trace 拒绝/重复终态均 0、checkpoint `ok`，后台结果 READY，流订阅 200。证据 `release-final-smoke.json`。真实 Chromium 首页跳转通过，390px 无横向溢出；截图 `output/playwright/phase6-6-login.png` 已视觉检查。
- 计划任务 **`Tiku Agent Phase6 8898`**，当前用户登录后启动，使用已安装的 PowerShell 7 路径及固定 release/manifest。已实际从计划任务启动、接管原监听进程，并在新版经过两个以上 20 秒健康周期；未声称验证无人登录冷启动。
- 发布时新版 Python PID **13152**；8790 **25988**、8896 **24180** 均保持原 PID（这些数值仅为本次证据，维护时必须重新核验）。计划任务 XML、两个 manifest、代码 bundle、启动前控制库、离线全 runtime 和完整性摘要均保存在上述仓库外备份目录。
- 仅本地提交与独立发布；未推送、未合入主线。新服务将来产生费用/未决任务后，应继续保留当前执行库和费用证据，以兼容状态的版本维护，不能覆盖回旧快照。


## 产品体验修正（2026-09-13）

用户指出阶段 5 已隐藏的“任务状态与控制”在阶段 6 被重新显示，以及处理气泡只显示固定文案。本次恢复原产品要求：CSS 不再因后台模式显示控制面板，DOM/新对话协调及后台恢复能力保留；诊断浏览器脚本必须显式注入样式才能使用独立面板。

后台 worker 复用旧流接口 `_public_progress_event` 的公开白名单，只保存经过校验的阶段文案、已知章节或有界校验计数。dispatch v1 增加兼容列 `progress_message`，启动时在事务内补列，保留原操作、队列截止、费用和状态；读出时再次按同一白名单校验。网页将最新步骤传给原有处理中气泡，不再统一改成“任务正在后台处理…”。仍按原键读取，观察不调用模型。

验证：`test_background_progress` 覆盖步骤/计数变化、重新读取、私有文案拒绝和旧表兼容升级；客户端契约增加步骤变化检查。真实浏览器 `phase6_progress_browser_acceptance.js` 的 8 项检查通过：气泡连续显示路线检查、整页理解、1/2、2/2，调用数保持 1；刷新及新对话后面板仍隐藏。截图 `output/playwright/phase6-progress-restored.png` 已视觉检查。最终全量回归 **1,728 tests / OK，279.486 秒，无跳过**；日志 `.tmp_tests/phase6_6/progress-full.log`。已发布固定版本 **`658f0ccd965eb8362a0a43822550a872acb4121c`**，8898 计划任务及 manifest 同步指向该版本。

发布前确认无排队、运行中或待交付任务，核验进程身份后平滑关闭 8898，为完整 runtime 建立离线备份 `F:\cc\_backups\7-题库检索\2026-09-13\phase6-8898\ui-progress-before-658f0cc`。上线验证健康状态为 `ok`，实际提供的 CSS/JS 与固定版本逐字节一致，旧表补列成功，SQLite 完整性检查通过；原有 6 个操作的状态和结果全部保留，执行效果与费用记录数量不变，未新增模型调用。证据 `ui-progress-smoke.json` 保存于同级备份目录。新版 Python PID 为 32584，8790/8896 仍分别为原 PID 25988/24180；看门狗连续健康检查通过。PID 仅为本次证据，后续维护须重新核验。

### 接收提示文案修正

按用户确认，将接收后的“任务已接收，正在排队…”简化为“任务已接收”，实际开始后继续显示既有步骤进度；同步更新脚本缓存版本。15 项客户端契约检查通过。8898 已平滑切换至固定版本 `53a53cbb58ee12dd8083ce9e1a2cd6d501e215fc`，实际返回脚本与发布文件一致，健康检查通过；原有 10 个操作及执行效果、费用记录保留，8790/8896 PID 未变。完整离线备份位于发布备份目录的 `acceptance-copy-before-53a53cb/runtime`，验证证据为 `acceptance-copy-smoke.json`。

### 独立验收问题修复（2026-09-13，仅本地）

修复接收被明确拒绝后仍阻塞原对话，以及旧 epoch 未完成记录累计 64 条后阻塞新对话的问题。提交接口为受控的接收前拒绝增加证明字段；网页核验后展示具体原因并解除本次意图和 fence，允许用户手动重试。未知接收、一般提交异常和 lookup 404 继续保留原键；已持久接收后的诊断失败不返回拒绝证明。新提交在共享 Web Lock 内取得匹配的服务端当前 epoch/version 后清理旧 epoch 的浏览器传输记录，保持服务端 UNKNOWN、取消及费用证据；晚到查询不能重新创建已退役记录。协议与保留规则分别见 6.3、6.4 的验收修正。

新增 `test_background_rejection` 的 3 项测试，覆盖真实满队列、输入/额度拒绝、提交后丢 ACK 及诊断失败；客户端契约扩展至 21 项，覆盖拒绝证明校验、64 条旧记录、过时绑定、当前 pending 保护、晚到观察和存储失败。真实 Chromium 新增 `phase6_admission_browser_acceptance.js` 的 8 项检查通过；原 `phase6_browser_acceptance.js` 更新旧文案断言后 19 项检查通过，六次业务提交仍只有六次调用和 attempt。浏览器夹具和端口均独立，测试结束已关闭，新增付费模型调用为 0。

网页 JS 缓存地址及三个固定地址断言同步更新，JS 语法、CLI 帮助和 `git diff --check` 通过。此批不改变 CLI/飞书/检索 Skill 的入口或检索排序逻辑，不修改 live 数据、不合并或推送，不更新 8898 固定 release；生产运行版本仍为上一节记录的 `53a53cb`。

最终全量回归：**1,731 tests / OK，235.219 秒，无跳过**。日志：`C:\Users\31492\AppData\Local\Temp\phase6-fix-162fee6eba20444f9500ce1192becc63\regression-final.log`。可重复运行 `python -B -m unittest discover -s tests -q`；浏览器以 `python -B -m tests.phase6_browser_fixture` 启动新的隔离夹具，登录 `/fixture/start` 后依次执行上述新增和原有两个浏览器脚本。

### 验收修复发布至 8898（2026-09-13）

用户明确授权后，将上述修复发布至固定目录 `F:\cc\_deploy\7-题库检索\8898\200fbd839fa187d311a4975a23ce12f3272d5dd9`。发布前确认无 REGISTERED/RUNNING/UNKNOWN、等待派发、待交付或费用待确认项；核验完整 Python 与看门狗参数、计划任务和监听归属后，禁用启动任务并仅停止目标看门狗，再向独立 console 中的原服务发送 SIGBREAK。日志确认 `Application shutdown complete`，未强杀业务进程。

完整离线 runtime、原/新 manifest、计划任务 XML、源码 bundle、状态指纹、完整性检查和上线证据保存在 `F:\cc\_backups\7-题库检索\2026-09-13\phase6-8898\admission-fix-200fbd8`。备份 ACL 仅当前用户、SYSTEM、Administrators。原有配置只复用经检查的 root/top_k/answer_output 三项，不复制秘密。离线与上线后的 7 个 SQLite 数据库 quick_check/foreign_key_check 均通过。

计划任务及 manifest 已切到 `200fbd8`，新服务 PID 33924，完整启动参数与固定源码核验通过。健康为 ok，首页跳转邀请登录正常；实际返回的 background_jobs.js/demo.js 与固定 release 逐字节一致。操作、尝试、效果、费用、文件、派发、结果和媒体表的内容指纹与停机前一致：13 个操作、13 个 attempt、57 个效果、29 个费用运行及确认 outbox、11 份 READY 结果均保留；上线检查未新建业务调用。证据为备份目录中的 `release-smoke.json`。

新看门狗连续两个健康周期通过（16:36:15、16:36:36）；8790/8896 保持原 PID 25988/24180，未操作飞书服务。PID 与时间仅为本次证据，后续维护重新核验。仍未合并主线或推送；回退不得用旧快照覆盖发布后新增业务或费用证据。
