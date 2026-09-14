# 阶段 6.5：异常、重启与生命周期

日期：2026-09-13。分支：`codex/phase6-background-execution`。
状态：**6.5 DONE**。实现、集中验收、真实浏览器和全量回归均已完成。仅隔离 worktree 本地开发，未推送、合并或发布。
总体契约见 [阶段 6 计划](phase6_execution_plan.md)，网页恢复见 [6.4 交付](phase6_4_web_recovery.md)。

## 本批行为

1. **授权持续约束写入。** 后台 writer 除领取、模型 prepare/send 外，在租约续期、状态/文件收据写入和最终结果提交时重验持久 grant、实时身份版本、session/epoch 与 producer。授权读取失败同样拒绝。检查后的时间再次用于租约裁决，不能用耗时读取前的旧时间。写入检查不重新计算额度，已发送调用的证据与费用仍可结清。
2. **关闭优先于尚未完成的准入。** 若关闭发生在授权或存储检查期间，新接收在登记前拒绝，新领取保留原 WAITING 记录及原截止时间。已经发送的同步 provider 有界 drain，超时如实报告 `drained=False`，不伪装成已退出；释放后原 worker 不领取下一条。新实例在原期限和原容量内恢复待领取任务。
3. **UNKNOWN 不自动重发。** 已领取任务的租约到期后仍按原 token/attempt 转 UNKNOWN。PREPARED 的未发送证明、SENT/未确认响应、CONFIRMED 的已知用量分别核对；只有证据允许的本地费用补写、父子提交及逐题收据恢复可以继续，不生成第二次模型调用。原输入与未结清证据保留，观察不续排队期限、不执行恢复。
4. **显式收据恢复同步原任务状态与唯一回复。** 既有 `/api/execution/recover` 继续受当前 identity/epoch/revision、原 producer、文件摘要、父子及 unit 关系约束。恢复后原业务 operation 和 dispatch 同步到完成状态，进度版本增加、旧错误清除；publisher 仅按原收据补交付，Response 仍绑定原 operation。后台模式下控制 ACK 不另建可评分回复。网页先持久保存原任务的只读交付记录，再清除控制请求记录；ACK 丢失可刷新后沿原控制键核对。UNKNOWN 提示由原回复替换，裁图先于回复，存储中验证同一 Response ID 后才确认交付。重复同键恢复不增加原 attempt 或费用。
5. **取消不等于证据结清。** UNKNOWN、活跃输入以及存在未确认模型效果、未知用量、未结清费用或未完成文件收据的取消任务，继续保护私有 BLOB 和物化题图。扫描最多 1,000 个候选、每次删除最多 100 个已过 TTL 且证明可清理的输入，不以扩大存储或无限续期绕开容量门。
6. **维护与公共结果保持一致。** 自动和人工执行历史清理均保护仍在保留期内的公共结果；晚修复的结果不能仅因原 operation 较老就被删。人工 cleanup plan 的源摘要纳入已存在的 dispatch、登录绑定、publication 与媒体表，BLOB 用长度和摘要参与核验。核对后证据变化会拒绝旧计划，删除前仍须备份。
7. **版本/配置不兼容明确拒绝。** 重启只领取匹配 producer、完整输入、有效授权/期限的原任务。未知 dispatch/publication schema 或不同队列策略拒绝初始化；已有后台数据库不能在 `background_execution=False` 下静默启动网页。没有自动迁移旧 producer，也没有把 UNKNOWN 改回队列。

本批不改变检索识别、章节约束、排序、答案、模型并发和费用计算规则。CLI 维护参数与 plan → confirm → execute 流程不变，帮助说明扩展为阶段 5/6；飞书、题库 Agent 检索 Skill 和生产 launcher 无需新增入口或配置。

## 验收与证据矩阵

新增 4 个测试模块共 23 项测试，并向已有 A3 HTTP 模块增加 1 项恢复交付测试；客户端契约扩展至 14 项检查，控制客户端增加交付落盘前保留 journal 的断言。参数化子场景分别注入真实故障。使用隔离 SQLite、私有图片和可计数的合成 provider，不读取真实题库或付费模型配置。

| 要求 | 当前证据 | 判定 |
| --- | --- | --- |
| F12 未领取重启 | `test_background_processes`：接收后子进程 `os._exit`；新 worker 从原文本/图片 BLOB 自动执行；原截止时间不变 | 单 operation、单 attempt、单调用；损坏/缺失载荷、过期、撤权和 producer 漂移零调用 |
| F13 领取和模型 crash 窗口 | 同模块：领取后、PREPARED、provider 已接收、响应确认前、CONFIRMED 后直接退出 | 旧任务 UNKNOWN 不重排；未发送不捏造用量，已确认仅补一次费用，未知用量保持待核对 |
| F06 业务/交付提交窗口 | 同模块：业务提交后及 Response 提交后直接退出；重启 publisher | 只修复交付；相同 Response ID、1 次调用/attempt、120 个合成 tokens 的唯一费用记录 |
| F05/F14 父子与逐题恢复 | `test_background_a3_recovery`：child 保存后、parent 应用后、一个 unit 完成但下一个未提交、全部 unit 完成而父级未收尾 | 准确复用父子/unit 收据；未提交 unit 保持人工处理，重复恢复零调用/费用增量 |
| F13/F14 不能恢复的 A3 | 同模块：缺失 child 收据、第二 unit 已发送但未确认、恢复前 producer 改变 | UNKNOWN 不伪装成功，已确认的兄弟 unit 不证明未知 unit 成功，拒绝跨版本写入 |
| F09/F10 注销/过期/撤销 | `test_background_http_lifecycle`：排队/运行中实际 HTTP logout、控制库禁用/归档删除、grant 到期、session 到期 | 旧 Cookie/任务查询失效；排队零 attempt，运行中已发送调用费用保留，晚到结果不写回 |
| F10/F11 写入/授权读取失败 | `test_background_lifecycle`：最后一次调用后撤权、禁用、控制读取异常、会话过期；既有 `test_execution_dispatch*` 的队列额度/待对账/prepare→send 再校验 | 保留已知费用，阻止后续模型调用和当前态写入；未绕开原费用准入门 |
| F15 输入/清理/容量 | `test_background_lifecycle`：取消后未知效果超过 TTL，晚修复结果保留，旧 cleanup plan 遇证据变化；process 模块：物化后死亡及图片损坏 | 私有输入和物化证据保留，旧计划拒绝；现有容量/磁盘失败、图片超限及过期交付测试保持通过 |
| F17 shutdown/restart 竞争 | lifecycle 模块：关闭与出队授权竞争、有界 drain 后恢复原队列；process 模块：两个新进程同时领取 | 关闭赢得领取竞争后不新增调用；无第二个生效 token/attempt，队列未被静默丢弃 |
| F17 版本和隔离 | lifecycle/http 模块：schema、策略和禁用后台开关；process 模块：独立 loopback HTTP 哨兵在竞争进程退出后仍响应 | 不兼容启动拒绝、持久输入不变；只管理测试自身精确进程，不按端口停止服务 |
| F14/F16 恢复回复与反馈 | `test_background_http_a3` 及真实 Chromium 脚本 `phase6_recovery_browser_acceptance.js` | 控制 ACK 不创建第二个评分对象；丢 ACK 后刷新恢复沿原键，历史、第二标签页和两次反馈绑定原 Response，零模型调用增量 |

既有 6.2/6.3 的断连、容量、费用及 6.4 浏览器证据继续保留，新增测试未删除或放宽旧测试。上述证据覆盖 6.5 的异常/生命周期要求，不代表 6.6 的 F01～F18 统一发布门、真实模型样本和发布授权已经完成。

## 验证记录

- 集中回归：12 个新旧相关模块，**130 tests / OK，83.801 秒**，日志 `.tmp_tests/phase6_5/targeted.log`。
- 内核离线探针：1 operation、1 attempt、1 次调用、费用 CONFIRMED，重复读取 3 次，drained=True。最终证据 `.tmp_phase6_runtime/probe-ba37e25e1ef34a09802e65f3d68cadd6/`。
- HTTP/ASGI 离线探针：消费链断开后 SUCCEEDED/READY，1 次调用、1 个 Response，重复查询 3 次，worker/publisher 均排空。最终证据 `.tmp_phase6_runtime/probe-9b8bc849e2834644885ba5cc0c0ef0af/`。
- 真实 Chromium：`phase6_recovery_browser_acceptance.js` 的 **9 项检查通过**；3 次原始调用，恢复及多次刷新零增量，重复同键控制只有 1 个 operation，两次评分绑定同一 Response。日志 `.tmp_tests/phase6_5/browser-recovery-final.log`，截图 `output/playwright/phase6-5-recovery.png` 已查看。模拟断掉恢复 ACK 的网络错误属于测试预期；测试浏览器和本次独立服务已关闭。
- 最终全量回归：**1,720 tests / OK，235.909 秒，无跳过**，日志 `.tmp_tests/phase6_5/full-regression-final.log`。包含恢复交付修正后的全部代码；6.4 曾记录的单进程 Trace 排空时序失败本轮未复现，本批未放宽该断言。
- `scripts/search_by_loads.py --help`、`search.py --help`、`scripts/tiku_execution_maintain.py --help` 均通过；JS 语法检查和 `git diff --check` 通过。原始日志、数据库、图片及备份均不提交。

可复验命令：

```powershell
python -B -m unittest -q tests.test_background_lifecycle tests.test_background_processes tests.test_background_a3_recovery tests.test_background_http_lifecycle
python -B -m unittest discover -s tests -q
python -B scripts/run_phase6_kernel_probe.py
python -B scripts/run_phase6_http_probe.py
python -B scripts/tiku_execution_maintain.py --help
python -B -m tests.phase6_a3_browser_fixture --recover-child
```

最后一个命令启动仅用于验收的合成 A3 夹具，输出随机 loopback 端口及合成图片路径。将图片复制到 `.tmp_tests/phase6_5/synthetic-page.png`，用独立 Playwright CLI 会话打开该端口的 `/fixture/start`，运行 `run-code --filename tests/phase6_recovery_browser_acceptance.js`。每次完整复验使用新的夹具状态；只关闭该命名浏览器和本次创建的精确服务进程。

## 版本切换与后续边界

发布时先停止旧实例接收/领取，查看 worker 与 publisher 的真实 drain 结果。`False` 不构成安全切换证明；不要停止无关服务。未排空或 UNKNOWN 的输入、执行库、费用库、Response、媒体和 Trace 必须保留，使用原权威核对，不从浏览器重发。

新实例的执行/派发/publication schema、producer 和队列策略必须匹配；不同版本需要单独的兼容/迁移方案。后台数据库不能回交给未启用后台协议的旧程序。回退必须使用版本匹配的整套 runtime 快照，不能只回退代码或费用库，否则可能丢失已发送调用证据。6.6 再准备固定 release、完整备份及获准的精确进程切换；本批未实施任何发布或回退。

不得用旧快照覆盖备份后新增的调用或费用证据；这类情况需在独立目录核对最新证据和回退状态，不能把恢复旧数据库当作调用未发生。

继续沿用 `scripts/tiku_execution_maintain.py` 的 `inspect`、`cost-plan`/`cost-apply`、`cleanup-plan`/`cleanup-apply`。apply 必须核验计划 hash 并显式指定独立备份目录；项目实际维护备份默认放仓库外 `F:\cc\_backups\7-题库检索\<YYYY-MM-DD>`。本文没有授权对 live 题库或生产 runtime 执行清理。
