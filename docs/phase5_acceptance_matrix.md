# 阶段 5 验收证据矩阵

本表按 [阶段计划](phase5_execution_plan.md) A01～A18 核对本地证据。状态“已有证据”表示相关断言已定位和检查，不单凭测试名称或全仓通过数认定。各门补充验证及最终全仓均已通过，5.1～5.5 完成；2026-09-10 已按单独授权发布到 8790，见 [发布记录](phase5_8790_release.md)。

测试目录均为 `tests/`。公共入口使用真实 FastAPI/ASGI 处理、独立 runtime 和 SQLite；外部模型是可计数替身。进程中断证据另外使用独立 Python 进程与 `os._exit`，不能用 ASGI 线程测试替代。

2026-09-10 独立复验发现并修复两项遗漏，补充 A14/A18 证据：`test_execution_cost_admission.py` 验证 A2/A3 排队期间新增待对账时零业务/模型执行，补账后原键继续，并核对跨连接抢占和模型 prepare/send；`phase5_hidden_reset.js` 在产品默认隐藏样式下验证可见核对入口与刷新后新对话按钮复用原命令，5 次请求只形成 3 次重置且模型调用为 0。既有诊断面板浏览器脚本不代替本次默认样式验证。

| 门 | 已核对的证据与具体断言 | 状态 |
| --- | --- | --- |
| A01 | `test_execution_http_concurrency.py` 四种图片/文本、JSON/stream 双向竞争：两个独立应用共享库，阻塞第一个 provider 后第二请求返回 BUSY，只有一次 attempt；完成后重发同 operation_id，调用不增加。`test_execution_operations.py` 覆盖连续重发。 | 已有证据 |
| A02 | operations 的 A3 HTTP 全流程测试逐项更改图片字节、crop bounds、选题/准备 unit、候选 generation；同键冲突且原 observer/answer 调用数不变。文本和 state_version 改变同样冲突。 | 已有证据 |
| A03 | operations 覆盖身份和 session 错配、其他会话查不到 operation、缓存成功后撤销认证返回 401，业务调用仍为 1。 | 已有证据 |
| A04 | HTTP 并发测试在原收据重发后，以新键提交相同图片/文本；新 operation_id、两次模型调用、两条费用共 26 token。operations 检查 previous_operation_id 指向原操作。 | 已有证据 |
| A05 | state 的 reset/旧快照 CAS 与清空槽位重建测试；operations 的旧新操作拒绝、重复 reset 不清新任务；既有 task_state/前端动作矩阵覆盖旧 unit/revision/generation。 | 已有证据 |
| A06 | processes 的独立 runtime 进程竞争记录实际 provider 请求，只有一个 winner/attempt；operations 独立进程抢占；state 两个独立数据库连接的旧版本写入拒绝。 | 已有证据 |
| A07 | processes 覆盖登记后直接退出、运行中失联后 UNKNOWN；operations 检查旧 token 的状态写、renew 和 finish 被拒绝。 | 已有证据 |
| A08 | processes 的 before_send/provider_received/before_confirmation/response_confirmed 故障窗口逐项计数；确认未发经审核结 NOT_SENT，不伪造响应；已发送未确认不重放。详见进程验收。 | 已有证据 |
| A09 | processes 的提交后退出模拟 ACK 丢失，原键重发不增加 provider/费用；handoffs 保存子结果后的恢复精确复用原 child 收据，恢复后原操作可重发。 | 已有证据 |
| A10 | handoffs 验证半成品不发布、子收据失败回滚状态、父提交失败保留子收据、两次答案复制互不覆盖；operations 注入 persist_media 失败后同键仍仅交付一次答案；retention 清理孤儿前备份且保留有效引用。HTTP 并发测试另删除成功结果图片：媒体 GET 404、JSON/stream 原键均 RESULT_UNAVAILABLE，状态/attempt/调用不变；恢复原字节后重发成功。 | 已有证据 |
| A11 | processes 三个父子提交窗口检查父/子/unit 关系、无子结果时不造完成、已有子结果恢复不重跑；handoffs 检查事务回滚及 CHILD_READY/PARENT_APPLIED/COMMITTED 交接。 | 已有证据 |
| A12 | handoffs 部分准备失败后仅失败 unit 再验证，计数 u1=1/u2=2；中断后复用两项 worker 收据，费用两条共 240 token；第二项未提交和缺结果分别验证，不自动整批重跑。 | 已有证据 |
| A13 | handoffs 阻塞真实 runtime 后停止，不等模型返回；晚到写入失败；结束本页与 reset 分开，旧命令重发不改新页。浏览器材料证明跨标签控制及刷新后同键命令重发。 | 已有证据 |
| A14 | effects 覆盖业务成功账本失败、费用提交 ACK 丢失、并发同 run 对账及内容冲突；maintenance 的审阅哈希、双库备份和来源漂移拒绝；release_rehearsal 真实 CLI 补账后原调用数/费用行数均为 1。 | 已有证据 |
| A15 | client_audit 的 18 个活动适配器 × 已知响应/未知传输，加实际 SDK MockTransport；effects 独立记录 schema 纠错的两次调用及费用，usage 缺失仍待对账。详见模型调用核查。 | 已有证据 |
| A16 | state/operations 检查时钟倒退、过期代次及容量拒绝；retention 检查活动/UNKNOWN/待费用记录保护、旧键清理后 STALE、review 变更/备份失败不删除；release_rehearsal 真实 CLI 按持久策略清理及恢复备份。 | 已有证据 |
| A17 | release_rehearsal 导入真实旧父/子 SQLite，核对标准化业务态、legacy_snapshot 和零伪造收据；无键 runtime 请求拒绝；新写前旧备份可读、新写后恢复已结账执行收据。migration_publish 覆盖异常/进程死亡/目标竞争。operations 全流程同时覆盖文本和 typed action。 | 已有证据 |
| A18 | operations/handoffs 检查 operation/session/control 读取不增加业务调用；HTTP 并发测试在启用模式重复读取 health/session/media，无新增业务调用。新增真实运行队列与流生成器取消测试：首请求阻塞，第二请求收到 queued 后关闭流，零调用/费用/业务状态写，原操作回 REGISTERED、旧 attempt 为 FAILED；明确重发原键后只执行一次。浏览器控制已验收，既有业务、章节、排序、答案和费用回归纳入全仓。 | 已有证据 |

## 本批结果

- 2026-09-10 两项缺陷修复后，费用准入/父子/调用/进程/运维专项 76 项通过（27.734 秒），前端/HTTP/操作专项 42 项通过（7.902 秒）。新增 7 项自动回归；费用写入进行中与落账失败分别有确定性断言。
- 上线前全仓 **1623 项通过，149.487 秒**：`python -B -m unittest discover -s tests -q`，退出码 0。默认隐藏页面的两条 reset 核对路径、原诊断面板的跨标签停止/收据恢复/控制重发均通过真实隔离浏览器复验。前一轮并发测试暴露的正常落账误挡已修复，没有放宽失败断言。上线回归另修复并发测试的随机锁槽冲突，以 `PYTHONHASHSEED=13` 验证。
- 开发保持阶段五独立工作区与临时运行库；8790 发布使用固定干净 release worktree 与已离线迁移的生产执行库。未额外调用真实模型、未推送。CLI/旧飞书未启用 execution observer，未增加其准入规则。

- 迁移发布、真实 CLI 演练、状态、父子、保留相关 60 项通过，24.764 秒：`.tmp_phase5_1/release_targeted.txt`。
- HTTP 图片/文本双向竞争、结果失效/重发、只读请求及启用模式队列取消 5 项通过，2.258 秒：`.tmp_phase5_1/http_concurrency_final.txt`。
- 前一批全仓 1612 项通过，160.954 秒：`.tmp_phase5_1/release_full.txt`；随后仅补充上述结果失效/读取/队列测试，未改运行时代码。
- 2026-09-09 全仓 **1613 项通过，154.404 秒**：`.tmp_phase5_1/phase5_final_full.txt`。命令 `python -B -m unittest discover -s tests -p 'test_*.py'` 返回 0、摘要 OK；本记录保留为上一批历史证据。

## 其他交付核对

- 身份/版本/事务/父子关系、容量数值和迁移规则分别落实于 ExecutionPolicy、execution_store、execution_operations 及 5.2/5.3 契约；V1 业务快照保持原义。无幂等键的直接调用、提前打开的 store 和未 attach 的新 store 均有拒绝测试。
- 隔离 launcher、迁移 CLI、运维 CLI 的 `--help` 已执行；实际 `execution_control.js`、`task_state.js`、`demo.js` 均通过 `node --check`。未启动默认 launcher，避免连接真实模型。
- CLI/旧飞书保留未启用执行作用域的原策略，标准新 Agent 的 runtime/HTTP 入口启用执行门；模型 helper 与共享复筛的作用域分支均有新旧模式测试。没有改写章节解析、候选排序或题库写入流程。
- 浏览器证据文件仍可读：跨标签停止时 provider 未释放、旧返回被拒绝；恢复前后识别数均 8；丢失响应两次请求只有一个命令；reset 轮换 epoch。390px 截图已再次查看，无横向溢出。
- 自动后台调度、断线后持续执行、暂停/继续、生产切换和真实模型费用采样不属于本阶段本地交付，不通过缩减 A01～A18 来替代它们。

引用：[发布演练](phase5_release_rehearsal.md)、[进程验收](phase5_process_acceptance.md)、[模型调用核查](phase5_client_audit.md)、[浏览器控制](phase5_browser_controls.md)。
