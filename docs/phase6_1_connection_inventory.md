# 阶段 6.1：连接依赖盘点与行为边界

状态：**6.1 DONE（盘点、行为规则、验收设计和离线基线完成；后台执行尚未实现）**。
日期：2026-09-12。源码基线：`c5ec4639b02962d7e723ebc5644d5d590fe508da`。
分支：`codex/phase6-background-execution`。
工作区：`F:/cc/_worktrees/7-题库检索/phase6-background-execution`。
后续实施及验收依据：[阶段 6 实施契约](phase6_execution_plan.md)。

## 1. 目标与本批边界

用户需要提交搜题后可以刷新、断网或离开页面，回来仍能查到原任务，避免重复上传和调用。
当前已有状态权威、幂等操作、执行锁、模型效果和费用收据，主要工程缺口是后台调度、可重建输入和独立的结果观察/交付。
本批不以“进程中仍有线程”作为后台执行已完成的证据。

线上断线失败的发生率、用户等待时长分布尚无本批统计，不能声称它已被证明是最大业务瓶颈。
本阶段是用户明确选择的可靠性工作；不扩大到检索算法、自主规划或暂停/继续。
Trace 分段诊断已由同一对话在 c5ec463 发布，但历史超时根因未定；它是独立观察项，不由阶段 6 顺带宣告修复。

本次仅建立隔离工作区、文档和离线验证。未启动服务器、访问服务端口、调用模型、复制生产配置或读写 live 数据。
新工作区的验证输出使用 `.tmp_phase6_1/` 与测试自有临时目录。
现有项目记忆/路线图包含本次分支开始前的历史状态；本批依据当前 Git 和源码核验，不进行上下文 Skill 的自动交接。

## 2. 各入口当前实际执行位置

| 入口/动作 | 当前路径 | 连接依赖与需改动的位置 |
| --- | --- | --- |
| 图片 JSON / stream | `fastapi_demo.image`、`image_stream` → `_handle_image` → A3/A2 runtime | 图片字节在请求闭包中，临时 incoming 文件在该执行路径创建；没有先持久化可领取的后台指令再返回接收凭据 |
| 文本、补章节、确认、候选取答案 | `message`、`message_stream` → `_handle_text` → `execution_entry` | 文本及 typed action 由请求闭包提供；HTTP 两种传输最终调用同类业务入口 |
| A3 选题 | `a3_select` / `a3_select_stream` → `select_unit` | 可继续 A2；必须保留 workflow/revision/unit 绑定，不能把重连解释为重新选题 |
| A3 准备多题 | `a3_prepare_stream` → `prepare_units` | unit worker 的确认收据已独立保存；整批调用仍由 HTTP 启动；后台不能丢掉逐题确认与父级收尾边界 |
| A3 人工裁剪 | `a3_crop_stream` → `handle_crop` | bounds、原图、裁图、unit 和父 revision 都属于待持久化指令；现有不可变裁图与校验摘要继续复用 |
| JSON 外层 | `run_coordinated_json_task` | `await asyncio.to_thread(execute)`；请求等待执行完成，已有同步线程不能因取消 asyncio task 就被硬停 |
| 流外层 | `coordinated_stream_response`、`_stream_agent_events` | async run task 和进度 Queue 属于此流；关闭流设置 delivery_cancelled、取消 async task，并清理未暴露 Response |
| 排队/准入 | `session_runtime._ExecutionGate.enter`、`AgentSessionRuntime._admit` | 内存队列；通过 `progress.cancelled` 撤销尚未执行的排队项，断线因此会改变排队任务生命周期 |
| 进程生命周期 | `fastapi_demo.create_app.lifespan` | 已管理会话清理、Checkpoint/Trace 关闭；尚无持久业务队列领取、任务 drain 或启动恢复扫描 |
| 观察、控制 | `GET /api/session`、`GET /api/execution`、execution control/recover | 前两者核对状态，不启动业务恢复；后两者是显式命令。现有执行视图主要列 pending 操作，不是完整的完成任务发现与结果查询接口 |
| 页面恢复 | `demo_web/demo.js`、`execution_control.js` | 有 Web Lock、pending fence、epoch/state_version 校验和显式重连；尚无独立后台任务订阅协议，不能仅加一个“刷新按钮”完成阶段 6 |

源码入口：[HTTP 与流](../tiku_agent/fastapi_demo.py)、[A2 runtime](../tiku_agent/session_runtime.py)、
[A3 runtime](../tiku_agent/a3_runtime.py)、[浏览器](../tiku_agent/demo_web/demo.js)。

## 3. 可以复用的权威与不能混淆的边界

| 已有能力 | 当前证据 | 阶段 6 的要求 |
| --- | --- | --- |
| 操作登记、抢占和心跳 | `execution_runtime.execution_entry` 在同一次调用内 register → claim → runtime → encode → finish；租约线程定期 renew | 拆开接收与执行，但保留同一套 operation/attempt/token 权威；不能在 worker 外再造一套能绕过它的执行状态 |
| 输入去重 | `execution_operations.OperationStore.register` 持久保存 fingerprint、kind、部分 target；`execution_runtime._input` 将 Path 转成内容摘要 | fingerprint 无法还原文本或图片；必须新增私有、有界、可重建的输入载荷与受控文件引用，不能从日志或 Checkpoint 拼回请求 |
| 持久结果 | `encode_response` / `decode_response` 保存安全业务收据、文件摘要；`OperationStore.finish` 保存 SUCCEEDED | 业务收据不等于已交付网页结果。后续还要保存稳定公共 Response/媒体关联，不因观察者掉线而丢失 |
| 原键重发 | 已成功操作由执行入口解码原收据，不重复模型调用 | 新查询接口必须是读取，不依赖再调用变更接口取结果；接收 ACK 丢失要能按原键查询 |
| 执行中断 | 租约超时 RUNNING 分类 UNKNOWN；效果 SENT/UNKNOWN 不自动重发 | UNKNOWN 不等于可排队任务。后台不得把过期租约一律改回 QUEUED |
| 父子恢复 | `execution_handoffs`、`execution_units`、`execution_commands._recover` | 复用已确认子 A2/逐题收据；只恢复现有证据能证明安全的部分，不承诺任意模型步骤断点续跑 |
| 配置与源码版本 | producer 覆盖源码及声明的 runtime 配置；发送、写回、恢复均检查 | 新版本不能领取旧版本任务后静默按新配置执行；后台发布需要有界排空/兼容或明确拒绝方案 |
| 状态与权限 | `ExecutionStore` epoch/CAS、TaskStateSnapshotV1、branded 动作 | 后台调度状态只用于运行展示；不取代业务状态、不因进度文案或 next_stage 授权动作 |
| Trace/Response/Checkpoint | HTTP Trace 终态绑定连接结果；流关闭会 discard_unexposed | 拆开 HTTP 接收/观察终态和后台任务终态。只断了观察连接，不能把搜题记成业务失败；证据队列不是业务调度队列 |
| 费用、停止与 reset | `ensure_cost_available`；显式控制绕过旧请求长期持有的协调门 | 接收、出队、模型 prepare/send 继续复核额度/待对账；控制也必须幂等，旧 epoch 的晚到结果不得恢复当前任务 |

源码：[执行边界与编码](../tiku_agent/execution_runtime.py)、[操作权威](../tiku_agent/execution_operations.py)、
[状态权威](../tiku_agent/execution_store.py)、[控制和恢复](../tiku_agent/execution_commands.py)。

已核验的当前默认值（不是本批新增配置）：会话 TTL 2 小时，执行租约 300 秒、单次最长执行 1800 秒；
单结果上限 256 KiB、总结果上限 32 MiB，上传图片上限 15 MiB。
来源：`conversation_ttl.py`、`ExecutionPolicy`、`fastapi_demo.MAX_IMAGE_BYTES`。
8790 的 1 运行/2 排队/55 秒配置来自已核验的既有发布；阶段 6 不擅自放宽。

## 4. 行为规则的关键取舍

1. 后台 job 的单位是**一次用户操作**，例如上传、补充、选题或裁剪；不是无条件一路运行到最终答案。
   返回需补章节、等待选题、人工裁剪或候选时，本次操作完成，等待新的明确操作。
2. “已接收”必须晚于持久化指令、可用输入和原子队列容量准入；不能只启动线程就返回接收成功。
3. 已接收后关闭网页不取消排队/运行；排队仍受原等待期限限制，重连不延长排队或授权期限。
4. 页面断线和服务器死亡分开验收。只自动领取完整、有效且确认未开始的队列指令；UNKNOWN 保持核对边界。
5. 观察必须校验身份、会话和 epoch。使用同一个邀请码不等于能读其他会话的任务；operation_id 不是访问凭证。
6. 会话过期、重置、注销和撤销权限分别处理，见实施契约；不会以“后台继续”为由无限续期或复活会话。
7. 推荐用现有 SQLite 执行权威加受控派发记录和进程内后台 worker，先按单服务进程落地。
   单纯内存队列更简单，但不能兑现持久接收；先做 A2 纵向切片更快，但不代表 A3 已交付。
   当前不需要为此引入新的队列服务或自主规划框架。

## 5. 6.1 验证证据与完成界限

本批选取以下 12 个模块在新 worktree 执行，模型使用测试替身，不启动监听端口：

```powershell
python -B -m unittest -q tests.test_execution_http_concurrency tests.test_execution_operations tests.test_execution_state tests.test_execution_processes tests.test_execution_handoffs tests.test_execution_effects tests.test_execution_cost_admission tests.test_execution_frontend tests.test_execution_versions tests.test_tiku_agent_session_runtime tests.test_tiku_agent_fastapi_demo tests.test_demo_web_task_state
```

结果：**277 tests / OK，无跳过，44.049 秒**。日志 `.tmp_phase6_1/baseline.log`，不提交日志。

关键用例已核对断言内容：

- `test_enabled_runtime_stream_cancel_withdraws_queue_without_call_and_original_key_can_resume`：排队流关闭后原操作回 REGISTERED、attempt FAILED，零调用/费用/状态写；显式重发后仅执行一次。这是**当前行为证据**，阶段 6 新入口应改成“已接收后断线不撤队”，旧入口兼容范围单独明确。
- `test_cancelled_stream_worker_keeps_its_session_gate_until_true_exit`：async consumer 取消不代表同步 worker 已退出。
- `test_process_death_after_send_keeps_unknown_and_does_not_repeat`、`test_process_death_after_commit_replays_without_new_call_or_charge`：区分未知效果与已保存结果。
- `test_explicit_recovery_finishes_parent_without_rerunning_child`、逐题恢复用例：父子收据恢复边界。
- `test_a2_queue_rechecks_and_keeps_unsent_operation_retryable`、`test_a3_queue_rechecks_before_workflow_or_model_starts`：出队重新准入，不绕过待对账。
- `test_expiration_rotates_epoch_and_clock_rollback_fails_closed`、前端恢复用例：过期、旧动作与页面核对边界。

6.1 的完成表示分支隔离、现状证据、行为规则、分步交付和未来验收矩阵均可复核。
下文 F01～F18 是阶段 6 **待实现/待验收** 的门，不因为上述阶段 5 基线通过而标为完成。
