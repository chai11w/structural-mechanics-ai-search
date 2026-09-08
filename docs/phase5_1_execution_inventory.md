# 阶段 5.1：执行现状与缺口清单

状态：**DONE（盘点、行为规则与验收设计完成；不表示阶段 5 执行能力已实现）**。

核验日期：2026-09-08。源码基线：`f9e45bd`（包含生产代码基线 `066c967`）。
专用分支：`codex/phase5-idempotent-execution`。
工作区：`F:/cc/_worktrees/7-题库检索/phase5-idempotent-execution`。
本次只新增开发文档并更新路线索引，未启动服务、调用外部模型、读写生产库或推送。
后续工作和验收以 [阶段 5 实施规则](phase5_execution_plan.md) 为准。

## 1. 目标与判断

目标是让同一逻辑操作在重复提交、并发、失败与进程重启后有可核验的执行归属，避免重复副作用、重复记账及父子状态错绑。

已验证：现有系统有 HTTP 请求栅栏、前端恢复保护、动作身份校验、会话锁和费用主键约束；不是从零建设防重复。
已验证：这些保护尚未组成持久的逻辑操作收据、通用状态版本和可恢复父子执行记录。
尚未验证：线上重复执行/重复扣费的发生率，以及它是否为当前最大业务瓶颈。离线缺口不能替代线上事故证据。
因此 5.1 先明确边界；5.2 起按小步实现，不以本阶段为理由引入后台调度框架或重写检索算法。

## 2. 入口与操作清单

下表是当前代码事实；“所需规则”是后续目标，不是现有保证。JSON 与 stream 是同一业务操作的两种传输方式。

| 操作 | 当前入口与执行位置 | 输入/目标 | 当前副作用与重试行为 | 所需规则 |
| --- | --- | --- | --- | --- |
| 上传整图/单图 | `POST /api/image`、`/api/image/stream` → A3 `handle_image` 或 A2 `handle_image` | 会话、图片；HTTP 带协调 fence | 保存图片；A3 清理旧当前态、建立新 workflow，识别/裁图；可能下行 A2；重复直接调用会新执行 | 同次上传重发沿用操作键；主动上传同内容仍可成为新任务 |
| 文本指令/补章节/确认字母库/选内部题目 | `POST /api/message`、`/api/message/stream` → `handle_text` → Agent dispatch | 文本、当前会话；部分按钮带 `action_context` | 可能调用意图模型、修改状态、启动检索；自由文本无 typed context 时走当前态解析 | 文本也先登记一次操作；执行前绑定并复核解析目标；不只保护按钮 |
| A3 选题/换题 | `/api/a3/select`、`/api/a3/select/stream`，或文本 → `select_unit` / `_select_locked` | workflow ID、父 revision、unit ID | 校验旧目标；可能清旧子态、选中题目；已有合格裁图时进入 A2；内部路径对当前已选 unit 有保留进度分支 | 重复提交复用原操作结论；换题是独立操作；禁止旧子结果回写新 unit |
| A3 批量准备 | `/api/a3/prepare/stream` → `prepare_units` / `_prepare_units_locked` | workflow ID、父 revision、unit 集合 | 拒绝重复/已准备等非法目标；并发校验裁图，保存各 unit 结果 | 批次与 unit 子操作分别有归属；部分失败不能重跑已确认完成项 |
| A3 人工裁剪 | `/api/a3/crop/stream` → `handle_crop` | workflow ID、父 revision、unit ID、bounds | 写不可变裁图、保存 VERIFYING；裁图/外荷载校验；通过后保存 A2_ACTIVE 并下行；校验异常可回到裁剪态 | 同键同裁剪不能重复校验；新 bounds 是新操作；超时不能直接认定没执行 |
| A2 搜索/显式重试 | `handle_image`、`handle_prechecked_image`、`handle_preanalyzed_image`；文本/typed `retry_search` → `_start_image_search` | 当前图/上下文、章节和任务身份 | 识别、路由、粗筛、复筛、候选保存；同图重试可沿用 search ID，但增加子 task revision | 区分请求重发与用户明确重新搜索；尝试记录独立，模型与工具调用受同一操作约束 |
| 选候选取答案 | message 的 typed candidate action，或文本 → `_answer_candidate` | child ID、revision、candidate generation、rank | 校验候选；读取题库答案并复制到 Agent 输出目录，保存 ANSWERED | 文件准备与交付分别记结果；重发不能意外重启识别/复筛 |
| 重发答案/候选 | message → `resend_answer` / `show_candidates`；媒体 GET | 当前已保存答案路径或候选 | Agent 分支复用已有路径；HTTP 做媒体持久化/交付；A3 有精确媒体失败回退 | 已存在且授权有效时仅重新交付；过期/缺失不能偷偷改用另一版题库答案 |
| 停止当前题/结束本页/返回列表 | message → A3 dispatch / A2 cancel | 当前父子任务及取消范围 | 更新父列表/phase，必要时清理子态；部分取消先追问；当前锁下执行，不保证中断在途模型调用 | 控制操作也要幂等；范围精确；结果晚到不能把已结束任务重新激活 |
| 重置会话 | `POST /api/reset` → `clear` | 当前会话身份 | 等待相关锁、清理父子状态与媒体 | 重置必须更换会话执行代次；旧重置重发不能清掉之后的新任务 |
| 恢复观察 | `GET /api/session` 与前端 `retryConnection` | 会话、待核对 fences | 原子读取当前父子快照；协调对账，不重放原 A3 请求 | 保持读/执行分离；查不到结果不等于未执行 |

证据入口：

- [HTTP 路由与协调门](../tiku_agent/fastapi_demo.py)：`coordinated_task_preflight`、`message`、`message_stream`、image/reset/A3 路由、`_validate_action_context`。
- [A2 runtime](../tiku_agent/session_runtime.py)：`handle_image`、`handle_text`、`_handle_text_or_authorized_action`、`_run`、`_admit`、`clear`。
- [A2 Agent](../tiku_agent/agent.py)：`_dispatch`、`_start_image_search`、`_run_search`、`_answer_candidate`。
- [A3 runtime](../tiku_agent/a3_runtime.py)：上述同名方法、`_dispatch_a3_intent`、`_after_a2_response`、`mark_media_delivery_failed_v1`。
- [答案工具](../tiku_agent/tools.py)：`answer_candidate_tool` 默认复制到独立 Agent 输出目录，不写原题库。

## 3. 已有保护、限制与缺口

| ID | 已有保护及代码依据 | 准确边界 | 后续归属 |
| --- | --- | --- | --- |
| G1 | `fastapi_demo._SessionCoordinationGate` / `_parse_session_coordination_headers`；Web Locks + pending fences | 同进程按 session 保存内存 fence，保留 2 小时；重放被拒绝，不返回原业务结果；新 gate 无旧记录。无头部且非同源浏览器的兼容请求可进入 unversioned 路径。fence 不绑定请求内容摘要 | 5.3 持久操作登记、统一各入口 |
| G2 | `AgentSessionRuntime._admit`、`_ExecutionGate`、A3 `_locks` | A2 为 64 条带 `threading.Lock`，A3 为 64 条带 `RLock`；提供进程内串行化/有界准入。不同 runtime/进程不共享这些锁；串行执行不等于去重 | 5.3 数据库原子争用与旧执行者隔离 |
| G3 | `state.start_search`、`set_candidates`；typed action 在 runtime 锁内重验 | 子 revision 在 start_search 递增，候选另有 generation；父 revision 在新图递增。不是每次状态修改都递增，也不能比较父子 revision 证明关联 | 5.2 通用版本/代次；保留现有字段含义 |
| G4 | `SQLiteSessionStore.save`、`SQLiteA3SessionStore.save` | 各为按 session 覆盖当前 JSON 的独立事务，默认滑动 2 小时 TTL；无 expected-version 条件更新。不是历次任务/尝试历史 | 5.2 状态版本和持久关系 |
| G5 | A3 selected/completed/searched unit 集合、`_select_locked`、`_after_a2_response` | 当前 child 按同 session 读取；`AgentState` 无持久 workflow/unit 外键。A3 保存 A2_ACTIVE → A2 自己保存 → A3 收尾不是同一事务。阶段 4 证据绑定不能当执行外键 | 5.2 关系记录，5.4 中断核对 |
| G6 | `SQLiteModelCostLedger.write_run` 的 run/call 主键及同一写事务 | 已拒绝相同 run/call ID 的重复插入；不是无条件可安全重放的 upsert。A2 `_run` 和 A3 `_call_model` 每次生成新 run ID；相同 request ID 不会复用 run ID | 5.3 操作→尝试→费用映射，5.4 幂等落账 |
| G7 | A2 `_run` 先处理/保存业务状态，finally 写费用；A3 `_call_model` finally 写费用 | 费用异常被捕获，不阻断业务；业务库、费用库和输出文件无共同事务。可能业务完成但本地账本缺失；不说明供应商没收费 | 5.4 待对账状态和可核对交接记录 |
| G8 | 前端恢复仅对账；stream 取消信号可撤销尚未执行的排队项 | `asyncio.to_thread` 已开始的同步工作不能靠取消 asyncio task 硬停；无独立持久任务调度器。JSON/stream 都不能据断连自动认定业务失败 | 5.4 不确定态；后台执行生命周期留给阶段 6 |
| G9 | A2 显式 retry、A3 `_retry_page_understanding`；`QwenA3PageObserver.observe_with_diagnostics` | 整页 parser 校验失败有最多 2 次生成的有界纠错；这是已收到无效结果后的新调用，不能与网络未知结果重放混为一谈。具体生产 client/SDK 的隐藏重试还需在 5.4 逐个确认 | 5.4 调用尝试和错误分类 |
| G10 | Trace/Response/Checkpoint 有关联、唯一终态/隐私投影、TTL 和容量约束 | 诊断保存允许丢失、过期，Response 可丢弃未交付记录，题库引用读取当前文件；都不是完整执行收据或可恢复结果缓存 | 5.3/5.4 独立执行权威和结果寿命 |

前端依据：[demo.js](../tiku_agent/demo_web/demo.js) 的 `withSessionRequestLock`、`retryConnection`、typed action 绑定；[阶段 3 契约](task_state_snapshot_v1_contract.md) 的“同 session 弱绑定”“task_revision 不是快照版本”和 refresh-recovery 章节。
费用依据：[model_costs.py](../tiku_shared/model_costs.py) 的 `ModelCostCollector`、`new_run_id`、`write_run`、`_create_schema`。

## 4. 离线核验结果

### 4.1 现有回归

在专用 worktree 执行以下命令，**314 tests / OK / 无跳过**。包含 Python 回归及 Node 前端 task-state 测试；不是本次新写的阶段 5 验收测试。

```powershell
python -B -m unittest -q tests.test_tiku_agent_session_runtime tests.test_tiku_agent_session_store tests.test_model_costs tests.test_task_state_runtime tests.test_task_state_builder tests.test_tiku_agent_fastapi_demo tests.test_a3_runtime tests.test_a3_a2_adapter tests.test_tiku_agent_8790_a3_v1 tests.test_demo_web_task_state
```

关键已有证据（可在对应测试模块按方法名定位）：

- HTTP：`test_v6_fence_replay_and_old_same_origin_page_fail_closed`、`test_v6_message_json_and_stream_ack_exact_fences_and_reject_replays`、`test_v6_a3_select_json_and_stream_use_the_same_coordination_gate`。
- 执行门：`test_execution_gate_cancelled_waiter_withdraws_before_execution`、`test_same_session_waiter_does_not_consume_global_runtime_permit`。
- 账本：`test_duplicate_run_id_is_rejected_without_mixing_call_rows`、`test_duplicate_call_id_is_rejected_without_reparenting_existing_call`、`test_cost_run_event_is_emitted_only_after_successful_transaction`。
- 父子/旧动作：`test_select_unit_rejects_old_workflow_with_same_revision_and_unit`、`test_prepare_units_rejects_duplicate_active_and_prepared_targets_without_model_work`、`test_child_action_is_revalidated_after_capture_inside_execution_lock`。
- 前端：`test_real_transport_lifecycle_updates_session_and_does_not_replay_a3`。

### 4.2 补充探针

在 `.tmp_phase5_1` 下用临时 SQLite/SessionArtifacts、`tests.test_tiku_agent_session_runtime.FakeTools` 和注入费用对象进行断言；所有模型/题库工具均为 fake。临时报告保留在本地 `baseline_probe.json`，不进入提交。

| 探针与重现步骤 | 实际结果 | 能证明/不能证明 |
| --- | --- | --- |
| 同 A2 runtime，连续两次 `handle_image`，相同 session、文件和 request ID；统计 fake 分析函数与 collector run ID | 分析 2 次、不同 run ID 2 个；两次均 WAIT_CANDIDATE_CHOICE | 证明 runtime 的 request ID 不去重；未经过 HTTP v6，不能声称浏览器连点一定重复执行；未产生真实费用 |
| 同 session 读取两份状态，第一份写 pending chapter，再用第二份旧状态写另一 pending chapter | 后写旧快照成功覆盖为“5位移法” | 证明 store 自身无条件版本写保护；不证明现有同进程锁内请求必然丢更新 |
| gate 对同 fence 调用两次，再创建新 gate 调用该 fence | admitted 为 true / false / true | 证明 fence 记录存在于 gate 实例内；不是完整重启 HTTP 烟测 |
| 注入 `write_run` 抛 OSError 的 ledger 后执行 fake 图片请求 | 写入尝试 1 次；回复与持久状态均 WAIT_CANDIDATE_CHOICE | 证明费用失败与业务成功可并存；不推断真实费用已丢或已扣两次 |

## 5. 各入口同步检查

- 新 Agent/Web：后续接入共享 runtime，同时覆盖 JSON、stream、typed action 和文本路径；直接 Python runtime 调用必须有明确适配策略，不能成为绕过入口。
- Launcher：`run_tiku_agent_8790.py` 使用共享 Web/A3 核心；`run_tiku_agent_8896.py`、`run_tiku_agent_demo.py` 组装父子 runtime/store。5.2～5.5 修改共享构造参数时同步检查这些 launcher。
- CLI/项目 Skill：`SKILL.md` 仍指向 `search.py`、`scripts/search_by_loads.py`、`scripts/multi_agent_search.py`；本次不改检索/排序/答案工具接口，不宣称旧 CLI 已具备新 Agent 幂等协议。
- 旧飞书：`feishu_tiku_bot.py` 和 `feishu_store_flow.py` 的事件/入库链独立；本阶段不接管旧服务，入库仍走 plan → confirm → backup → execute。
- 本次无共享检索逻辑变更，因此无需修改 CLI/飞书/Skill 正文。发现的既有无关文案不在本次修改范围。

## 6. 5.1 完成门

- [x] 独立 worktree/分支来自已核验主线；未携带主工作区未提交文件。
- [x] 操作清单覆盖图片、文本、选题、准备、裁剪、检索/重试、答案、取消、重置和恢复读取。
- [x] 区分已有保护、可复现缺口、未验证线上影响。
- [x] [实施规则](phase5_execution_plan.md) 写明操作身份、重发/重试、版本、执行互斥、费用、恢复和验收。
- [x] 314 项相关基线回归和 4 个补充探针通过；不把后续目标当作本次已通过的测试。
- [x] 核对 CLI、飞书、Agent、Skill 的适用边界；不改变生产和题库。

下一步为 5.2 的数据契约与迁移设计；本次授权止于 5.1。
