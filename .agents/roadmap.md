# 项目 Roadmap

## Goal

主线是把 `8790` A3-V1 做成邀请制 C 端可用的可靠搜题服务。A3-V1 的“权威分流 + 整页理解 + GLM 裁图 + 多题全部并发 Qwen 双门禁并自动打开选题继续页/单题自动下行 + 人工回退”已从 8896 提升到 8790；8896 保留同内核验收，8897 保留校验前多选回退，Paddle splitter 继续作为 V2 延后。`8794` 的有限自主升级继续暂停。

## Current Priority

- 工程化梳理优先于新增自主功能：新 Agent/Web 统一输出层以及 Trace 2.2～2.5 已完成，8790/8896 精确重启、无费用协议烟测、只读 live 对照和保留 dry-run 均已通过；阶段 3.1 契约、3.2 服务端权威快照生成和 3.3 JSON/stream 出口一致性均已在代码中完成，busy/queue 拒绝保持无状态；前端仍未接入，仍不动 8788/8794，也不提前混入阶段结果或暂停/继续。
- 3.3.1～3.3.6 已完成且尚未部署；73 项 task-state、179 项直接相关及全仓 1137 项回归通过。主阶段 3 仍为 `IN_PROGRESS`，下一步是独立实施 3.4 前端消费，之后才进入含 8896/live/8790 的 3.5 启用门。
- 8790 网页已由 A3-V1 接管业务内核并继续使用 8795 控制库邀请码；费用、反馈汇总、动态额度和旧并发队列下一轮恢复。
- 8896 保留同内核验收，8897 暂留独立回退；当前重点是 8790 真实样本验收和失败记录收集。Paddle 试验结论保留，不再作为当前阻塞项。
- 8794 影子规划及有限自主阶段暂停排期；待 8892 固定流程、取消语义和验收样本稳定，且真实失败记录证明固定编排不足后再恢复。
- 网页产品化复用 8790 已验证能力；生产部署必须使用独立服务器配置与运行状态，不直接公开本机端口，也不影响现有飞书和本地服务。

## Non-goals

- 不重写检索、粗筛或视觉复筛算法。
- 不把所有请求改成 AI 规划，不开放自由多工具循环。
- 不把 8793 技术评审侧栏放进 8794。
- 不改造 8788，不自主化题库写操作。
- 不继续优化 Paddle splitter；GLM 有界整页裁图与 Paddle V2 保持分离。
- 固定编排稳定前不接入 LangGraph 或 DeepSeek Harness。
- 不为追求功能完整直接实现暂停/继续；每个前置阶段必须先独立改善现有系统的可理解性、可追踪性或稳定性。

## Delivery Stages

### 已完成：8794 隔离基线与能力提升（已进入 8790 生产）

- 已创建 `codex/mainline-bounded-autonomy-v1`；8794 使用独立端口、runtime、session、媒体、incoming、日志、Cookie 和启动入口。
- 状态感知安全回答 V0、五态工具结果和视觉方向校准均已在 8794 验收并提升到 8790；8794 继续承载后续自主能力开发。
- 8795 管理员后台闭环已完成：独立管理员 Cookie 与 `control.sqlite3`、邀请码生命周期和动态额度、费用/反馈查询、AES-GCM 可恢复副本、审计与看门狗；8790 已切换控制库模式。

### 当前：8890 复杂题图业务验证线

阶段状态以 [`docs/8890_complex_image_agent_plan.md`](../docs/8890_complex_image_agent_plan.md) 为准，此处只保留定位：

- Stage 1 数据契约与固定评测集：IN_PROGRESS
- Stage 2 预检影子运行：IN_PROGRESS（首批 6 条统计已出，尚未补费用报表）
- Stage 3 权威路由：已从隔离 8891 经 8896 提升到 8790
- A3 Phase 1 交接契约：DONE
- A3 MVP Phase 2 整页结构化理解与 parser：DONE（Prompt 四图回归通过；v2 parser 已通过真实输出和全量测试）
- A3 MVP Phase 3 人工裁剪交互：DONE（题目目录、选择、裁剪次数、先搜哪题、返回列表）
- A3 MVP Phase 4 单题 A2 交接：DONE（裁剪图绑定 `a2_context_text`，A2 重新判断章节、结构和荷载）
- A3 V1 GLM 整页裁图、多题全部并发校验并自动打开选题继续页、单题自动下行和人工回退：DONE（已提升 8790）
- A3 V2 Paddle 候选、自动裁剪和人工回退：DEFERRED

### 暂停：8794 影子规划与有限自主

- Planner 结构化计划、权限契约、有界执行、选择性自主等阶段在 8892 验证完成前暂停；恢复前以 8890 规范文档的验收门为准。

### 当前：Agent 工程化梳理与可恢复运行基础

目标是降低现有系统的理解成本、排错成本和改动风险；不以暂停/继续为理由一次性重写主线。各阶段按依赖顺序推进，前一阶段稳定后才进入下一阶段：

1. **统一输出层（DONE）**：新 Agent/Web 已把内部诊断与用户文案分离，注册错误码和白名单字段统一进入 HTTP、流式与 A3 公共输出；定向回归和 8790/8896 运行核验已通过，个人飞书入口保持原样。
2. **统一 trace、日志、错误和用户反馈（DONE）**：2.1～2.5 已完成；每次 HTTP/stream 操作可把路由、阶段、模型/工具、费用写入、错误、最终公共结果以及后续反馈串到服务端 trace。可评分回复先保存隐私受限的权威投影并获得 `response_id`，反馈必须以 `rated_response_id` 通过 identity/session/有效期及 conversation 目标一致性校验；独立诊断 CLI 已提供新链优先、旧链回退的只读查询和隔离的保留维护入口。详见 [`现状矩阵`](../docs/trace_phase1_observability_inventory.md) 与 [`Trace V1 契约`](../docs/trace_phase1_audit.md)。
3. **统一任务状态快照（IN_PROGRESS）**：为父 workflow 和子题任务提供权威的当前阶段、状态、已完成内容、允许动作和下一阶段视图，避免各入口自行拼装状态。
   - 3.1 冻结权威状态契约、公共结构、动作/里程碑矩阵、unit 互斥规则和 fail-closed 一致性边界：DONE；契约、定向测试和规范分别落在 `tiku_agent/task_state_contract.py`、`tests/test_task_state_contract.py`、`docs/task_state_snapshot_v1_contract.md`。
   - 3.2.1 纯构造器：DONE；从冻结 read-set 与可信入口证据无 I/O 投影 V1 快照，完成字段谓词、拓扑判断、动作过滤、17 个一致性 code 和 fail-closed 占位，定向 31 项及全仓 1015 项回归通过。
   - 3.2.2 锁内权威读取：DONE；A3 wrapper 按 A3→A2 锁序让父子 store 各读取一次，standalone A2 只获取 A2 锁，已持 A2 锁的调用方可传 frozen state 而不重锁/重读；缺失、不可读、稳定未知状态和受控文件/入口证据均已收口。47 项 task-state 定向测试及全仓 1031 项回归通过；当前只新增 runtime 内部入口，未改公共出口。
   - 3.2.3 异常与矩阵测试：DONE；实际构造结果已覆盖空记录与全部可持久化父 route/phase 合法/非法组合、九个 live child phase × standalone/direct A2/A3 active、特殊 child 边界、A3 active/父子组合读取异常、脱敏和旧 revision 动作证据。51 项 task-state 定向测试及全仓 1035 项回归通过；阶段 3.2 整体 DONE，未改生产代码或公共出口。
   - 3.3 JSON/stream 出口一致性：DONE，尚未部署。3.3.1～3.3.5 完成公共映射及 session、HTTP success/error、五条任务 stream 接入；3.3.6 已用非空 A2/A3 V1 验证跨出口 exact parity、完整 pair、失败后处理、零重读和 legacy 兼容，并修复 session/reset 不完整组合可能发布原 typed 或跨 read-set 拼接的问题。73 项 task-state、179 项 FastAPI/A3/Response Store 直接相关及全仓 1137 项回归通过。3.4 前端消费、3.5 启用门仍为 PLANNED，因此主阶段 3 保持 IN_PROGRESS，阶段 4～6 继续按依赖顺序保留。
4. **关键阶段结构化保存中间结果（PLANNED）**：保存识图、裁图、章节、荷载、分层候选数、失败原因及版本；以 `artifact_id` 受控关联短期原图、裁图和 overlay，查看、延长保留、删除均审计，路径不进 trace/公共输出，支持诊断、复用和 checkpoint。
5. **幂等执行与父子任务控制（PLANNED）**：统一任务版本、幂等键、执行锁和 A3→A2 父子生命周期，避免重复点击、网络重试或恢复造成重复执行和重复计费。
6. **长任务与 HTTP 流逐步解耦（PLANNED）**：让后台任务持有执行生命周期，HTTP/updates 流只负责提交、观察和控制；页面刷新或连接断开不再等于任务状态丢失。
7. **暂停/继续（DEFERRED）**：只有前述能力稳定且真实数据证明需求存在时，才在安全阶段边界实现 `PAUSE_REQUESTED → PAUSED → RESUMING`；已发出的模型调用不承诺中途冻结。

第 2 主阶段固定拆成 5 个小批次，避免把观测、修错和后台重构一次混做：

1. **2.1 现状盘点与契约（DONE）**：确认 8 套记录、实际 emitter、重复/丢失、反馈/费用 join、隐私边界和强制验收场景。
2. **2.2 Trace Context 与 ID 传播（DONE）**：服务端生成 `trace_id`，已加法贯通 HTTP、stream、线程、A3、A2、tool、task log 和 cost scope；费用 run/provider ID 已拆清，未改业务状态机。
3. **2.3 结构化事件与终态一致性（DONE）**：各运行根独立双写 `trace_events.sqlite3`，统一 request/route/stage/model/tool/cost/feedback 事件与 JSON/stream/媒体后处理的唯一 terminal event；严格白名单禁止原文和路径入库，请求线程仅有界入队，fail-open writer 通过 `/health` 暴露丢失、校验、写入和重复终态计数。
4. **2.4 权威响应与反馈绑定（DONE）**：JSON、stream、A3 父流程/A2 子题及可评分服务端错误在最终安全载荷形成后保存 `resp_...` 和隐私受限投影；同一 trace 同投影幂等、冲突拒绝。反馈 schema v8 必须提交 `rated_response_id` 和 conversation，服务端校验 identity、session、有效期、目标 message/response 一致性并从权威投影取协议与父子任务字段；旧 v7 反馈保持未绑定、只读兼容。
5. **2.5 Codex/Agent 诊断查询、保留与切换（DONE）**：独立只读查询库/CLI 已按 trace、response、feedback 和隐私安全的稳定 identity 输出“摘要 → 时间线 → 按需证据”的有界诊断包；诊断 8790 运行根时采用新链优先、旧链回退，业务服务不依赖诊断层。保留维护为独立 plan/apply 入口，默认 dry-run，未来时间只能 report-only，费用账本和管理员审计永不纳入。8795 仅可作为可选只读适配器，不是 trace/response 数据所有者、运行依赖或阶段完成门。

Trace/Response Store 与诊断层必须独立于 8795；任何可视化界面都只是只读消费者。

依赖关系固定为 `输出可控 → 过程可追踪 → 状态权威 → 结果可恢复 → 执行幂等 → 任务与连接解耦 → 可暂停/继续`。即使最终不实现暂停，前六阶段仍必须各自产生独立产品价值。

## Acceptance Gates

- 工程化整理不得改变现有正确的 A2/A3 业务输出、章节边界、候选排序和答案交付；每阶段使用现有固定行为和测试作为回归基线。
- 统一 trace 不得记录密钥、邀请码明文、完整模型原文或无必要的整段对话；问题案例只保留定位根因所需的图片、裁图、回复和反馈证据，并遵守过期策略。
- 在任务状态、阶段结果、幂等与后台生命周期未稳定前，不开放暂停/继续按钮。
- 8790、8793、8788 在开发期间持续可用，运行数据无交叉。
- 普通快速路径不调用 Planner，现有明确指令和按钮行为不回退。
- 禁止动作、越界参数、旧题/旧候选执行数为 0；写工具调用数为 0。
- 复杂样本同时报告直接完成、安全追问、可恢复、错误执行、额外回合与完整任务耗时。
- 8793 必须证明镜像来源 commit、行为一致性和独立 runtime；评审失败不得影响主线结果。
- 发布前保留 8790 可回退版本和运行数据备份。

## LangGraph / DeepSeek Harness Gate

只有 `8892` 数据证明固定编排不足，且确实需要跨请求暂停/恢复、人工确认、持久 checkpoint 或现有分支难维护时，才在后续独立阶段限时评估 LangGraph 或 DeepSeek Harness；迁移先做行为等价对照，不同时增加新自主能力。DeepSeek Harness 是 TypeScript/Cordis 技术栈的 developer preview，不能假定低成本替换现有 Python/FastAPI 产品外壳。
