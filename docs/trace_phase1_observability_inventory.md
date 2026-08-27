# Trace Phase 1 可观测性现状盘点

状态：2.1 实施前审计快照保留为基线；2.2 Trace Context、2.3 结构化事件/终态和 2.4 权威响应/反馈绑定已完成，2.5 诊断查询/保留/切换为 NEXT

日期：2026-08-27

范围：8790 新 Agent/Web、8896 验收入口，以及 8795 反馈管理/费用只读查询；8788 个人飞书入口不纳入改造

## 审计方法与证据边界

本盘点沿当前启动装配反推实际运行路径，再核对日志、SQLite、公共协议、错误处理、
反馈和后台查询代码。运行目录只检查文件名、大小和修改时间等元信息，没有读取日志
正文、反馈正文、媒体、用户会话或敏感配置。

主要证据：

- `scripts/run_tiku_agent_8790.py`、`scripts/run_tiku_agent_8896.py`、
  `scripts/run_tiku_agent_demo.py`：8790/8896/A2 的实际对象与目录装配；
- `scripts/tiku_agent_watchdog_8790.ps1`、`scripts/tiku_agent_watchdog_8896.ps1`、
  `scripts/tiku_admin_watchdog_8795.ps1`：进程输出和健康状态文件；
- `tiku_agent/session_runtime.py`、`tiku_agent/task_log.py`、
  `tiku_agent/a3_runtime.py`：任务摘要、A3 状态和页面错误；
- `tiku_shared/model_costs.py`：模型 run/call 与费用；
- `tiku_agent/fastapi_demo.py`、`tiku_agent/output_watchdog.py`：HTTP、流式、媒体、
  公共输出观察与异常漏斗；
- `tiku_shared/trace_events.py`、`tiku_shared/response_store.py`：结构化事件、终态和
  权威响应投影；
- `tiku_agent/demo_web/demo.js`、`tiku_agent/feedback_store.py`、
  `tiku_admin/reporting.py`：反馈提交、权威绑定、证据过期和费用关联；
- 相关协议、运行时、A3、FastAPI、输出观察、费用和后台测试。

静态代码可以证明错误出口和落盘规则，但不能穷举第三方库、操作系统、SQLite、网络和
模型服务未来返回的所有异常文本。本文件区分“注册的公共结果码”“实际错误漏斗”和
“无法穷举的底层异常”，不把它们混为一谈。

## 核心结论

1. 2.1 基线发现八套独立记录；2.3/2.4 又加了运行根独立的 Trace Store 和 Response Store。
   物理记录仍分散，但现在已有 `trace_id → response_id → rated_response_id/feedback_id` 的权威连接，
   下一步不是把所有文件强并成一个库，而是用 2.5 的只读查询层统一读取。
2. 8790 当前任务摘要写在 `a2/task_logs.jsonl`，不是根目录旧的
   `task_logs.jsonl`；当前共享费用写根目录 `model_costs.sqlite3`，不是旧的
   `a2/model_costs.sqlite3`。仅凭文件名很容易读错数据源。
3. 公共协议注册 100 个结果码，其中 23 个状态为 `ERROR`；但“注册”不等于当前每个
   码都有生产发出点，用户可见失败也可能是 `NEEDS_INPUT`、`NO_MATCH` 或 `PARTIAL`。
4. 历史 sink 仍可能重复或静默丢失；新 Trace writer 的 dropped/validation/write/duplicate-terminal
   计数已进入健康状态，但不会反向补记旧 sink。
5. 可评分回复现在先写 `responses.sqlite3` 的白名单投影，再返回服务端 `response_id`。反馈 schema
   v8 强制 `rated_response_id` 和 conversation，并校验 identity、session、有效期和目标
   message/response 一致性；旧 v7 行保持未绑定、只读兼容。
6. 路由、A3/A2、工具、模型、费用、最终公共结果和反馈现在能按 trace/response 权威串起；尚缺的是
   Codex/Agent 可直接使用的“摘要 → 时间线 → 按需证据”查询入口。8795 不是这些数据的所有者。

## 运行根目录与职责

| 名称 | 默认路径 | 职责 |
| --- | --- | --- |
| 8790 root | `.tmp_tiku_agent_v2_prod_8790` | 生产 A3 父流程、共享费用、trace、response、反馈、输出观察和服务日志 |
| 8790 A2 root | `.tmp_tiku_agent_v2_prod_8790/a2` | 当前子题 A2 会话、题图/媒体和任务摘要 |
| 8896 root | `.tmp_tiku_agent_a3_mvp_8896` | 隔离验收 A3、共享费用、trace、response 和输出观察 |
| 8896 A2 root | `.tmp_tiku_agent_a3_mvp_8896/a2` | 8896 子题 A2 会话与任务摘要 |
| 8795 root | `.tmp_tiku_admin_8795` | 控制库、管理员审计和后台服务日志 |

8790 与 8896 的用户会话、题目状态和媒体运行目录隔离；但 8790 与 8795 明确共享控制面：
8790 读取 8795 `control.sqlite3` 做认证/额度检查并更新邀请码使用状态，8795 还会直接更新
8790 反馈库的审核/归档状态及删除案例。8795 自己的管理员认证仍留在 8795 root。

## 当前记录生成矩阵

### 1. A2 任务摘要

| 项目 | 当前事实 |
| --- | --- |
| 生成器 | `AgentSessionRuntime._write_task_log` → `JsonlTaskLogger.write` |
| 触发 | A2 turn 的 `_run` finally；准入/额度/上传等 `AgentProtocolError` 由 `_admit` 记录；部分 HTTP 边界事件调用 `record_protocol_event` |
| 8790 | `<8790-root>/a2/task_logs.jsonl` |
| 8896 | `<8896-root>/a2/task_logs.jsonl` |
| 格式 | 一行一个 `TaskLogEntry` JSON |
| 主要字段 | `task_id/trace_id/request_id/search_id/session_key/identity_key`、kind、起止时间、阶段前后、outcome、题数、候选数、章节、route、`error_kind`、五态协议字段 |
| 内容边界 | 不保存原始用户文本、图片路径或异常正文 |
| 保留 | 只追加；没有滚动、大小上限或定期清理 |
| 写失败 | `_write_task_log` 捕获所有异常并静默继续，用户请求不失败，也没有第二处健康记录 |

任务摘要只描述一个 turn 的结果，不记录 turn 内的路由、每个阶段开始/结束、每次工具调用、
模型尝试和媒体后处理，因此不能作为完整 trace。

### 2. 模型费用与调用

| 项目 | 当前事实 |
| --- | --- |
| 生成器 | `ModelCostCollector`、`timed_model_call`、`SQLiteModelCostLedger.write_run` |
| 触发 | A2 turn 完成；A3 的分流、整页理解、框选、逐题校验、外荷载门禁和意图模型等各自完成 |
| 当前共享库 | `<8790-root>/model_costs.sqlite3`、`<8896-root>/model_costs.sqlite3` |
| 表 | `model_cost_runs`、`model_cost_calls` |
| run 字段 | `run_id/trace_id`、session/identity/search/task kind、起止时间、outcome、调用数、token、估算费用和 warning |
| call 字段 | call/run/trace/sequence、provider/model/call type/status、延迟、token、尝试数、`provider_request_id`、兼容 `request_id`、`error_kind` 和价格 |
| ID 规则 | 新 run 使用独立 `run_...`；供应商 ID 以 `provider_request_id` 为权威，旧 `request_id` 仅镜像兼容；历史行不重解释 |
| 保留 | 没有自动删除或滚动 |
| 写失败 | A2/A3 都捕获并静默继续；可能已经产生模型费用但账本没有对应 run |

旧的 `<root>/a2/model_costs.sqlite3` 仍可能存在，但当前 8790/8896 装配把同一个根目录共享
账本注入父 A3 与子 A2。后台为兼容历史会同时读取根库和 A2 旧库。

2.1 基线中 A2 曾把可复用的 HTTP `request_id` 用作 cost `run_id`，存在覆盖/混合风险；
2.2 已改为 `new_run_id()`。历史行继续只读兼容，不能据此把旧 provider `request_id` 当应用请求 ID。

### 3. A3 会话与页面错误

| 项目 | 当前事实 |
| --- | --- |
| 生成器 | `SQLiteA3SessionStore`、`A3MvpRuntime._record_page_error` |
| 位置 | `<root>/a3_sessions.sqlite3` |
| 会话表 | `a3_sessions` 保存整页状态 JSON、更新时间和过期时间 |
| 错误表 | `a3_page_errors` 保存 session hash、search ID、task kind、phase、error type/code/message 和时间 |
| 触发 | 整页理解、schema 尝试、重试、自动框选等明确调用 `_record_page_error` 的路径 |
| 保留 | 会话默认 2 小时；页面错误名义 30 天，但旧错误只在下一次写入页面错误时清理 |
| 写失败 | 诊断写入异常被静默吞掉 |
| 内容风险 | error message 最长 500 字符，保留异常链中的具体消息；该旧表本身没有 trace/统一白名单，2.3 另有不复制异常正文的事件层 |

`a3_sessions` 是当前状态，不是事件历史。`last_error/last_error_detail` 会被后续状态覆盖，
不能据此还原完整执行过程。

### 4. 公共输出观察

| 项目 | 当前事实 |
| --- | --- |
| 生成器 | `OutputWatchdog.observe` 的后台线程 |
| 位置 | `<root>/output_watchdog/output_watchdog.jsonl` |
| 触发 | 安全 HTTP 错误文案和最终 Web/A3 公共回复；普通流式进度不逐条记录 |
| 字段 | 时间、normal/awkward/dangerous、命中规则、长度、hash、脱敏 preview、intent、protocol code、媒体状态、endpoint、session hash |
| 安全 | dangerous 文本不保存原 preview；规则会遮盖路径、凭据特征、内部字段和 traceback |
| 保留 | 只追加；没有滚动或定期清理 |
| 丢失 | 队列上限 2048；满队列直接丢样本，目录/写入/观察异常均 fail-open 且无丢失计数 |

它回答“最终文案是否危险/别扭”，不回答“哪个模型、工具或阶段导致了结果”。
当前记录也没有 request/search/workflow ID；HTTP error 观察还会使用空 session，因此即使样本
存在，也只能靠时间和内容旁证，无法精确回连某次请求。
后台线程是 daemon，进程退出时没有显式 drain；队列尾部即使未满也可能随进程一起丢失。

### 5. Python stdout/stderr

| 项目 | 当前事实 |
| --- | --- |
| 生成器 | Uvicorn/Python logging、显式 `logger.warning/exception`、未处理 traceback、普通标准输出 |
| 8790 | `<8790-root>/tiku_8790.out.log`、`tiku_8790.err.log` |
| 8896 | `<8896-root>/service_logs/tiku_8896.out.log`、`tiku_8896.err.log` |
| 8795 | `<8795-root>/service_logs/tiku_admin_8795.out.log`、`tiku_admin_8795.err.log` |
| 格式 | 非结构化文本，由 PowerShell `Start-Process` 重定向 |
| 保留 | 脚本没有显式滚动、大小限制或隐私清理 |
| 关联 | 默认没有 request/search/workflow/trace 的稳定前缀 |
| 风险 | 完整 traceback 和第三方异常可能进入 stderr；既难查询，也可能含不应长期保留的信息 |

### 6. 看门狗状态

| 项目 | 当前事实 |
| --- | --- |
| 生成器 | 对应 `tiku_*_watchdog_*.ps1` 的 `Write-Status` |
| 状态文件 | 8790 根 `watchdog_8790.status`；8896/8795 各自 `service_logs/watchdog_*.status` |
| PID 文件 | 8790 根 `watchdog_8790.pid`、`tiku_8790.pid`；8896 `service_logs/watchdog_8896.pid`、`tiku_8896.pid`；8795 只有 `service_logs/tiku_admin_8795.pid` |
| 内容 | 看门狗启动、目标进程启动、健康通过、健康失败、端口进程停止/重启 |
| 频率 | 8790/8896/8795 均每 20 秒检查健康，但只在状态变化/启动时写行 |
| 保留 | 看门狗启动时 `Set-Content` 覆盖旧状态，随后 `Add-Content`；只保留本次看门狗生命周期 |
| 边界 | 进程级状态，不包含业务请求 |

PID 文件在启动时覆盖；进程异常退出后可能留下陈旧数字，只代表最近一次写入，不是进程历史或
存活证明。

### 7. A2 会话状态

`<root>/a2/session.db` 保存当前 Agent 状态，并由 FastAPI 默认每 300 秒调用运行时清理；会话和
题图/媒体默认两小时过期。它保存当前阶段、搜索、候选和最后错误等状态，但不是 append-only
日志。根目录旧 `session.db` 是此前装配遗留，当前 A3 包装下的 A2 状态以 `a2/session.db` 为准。

### 8. 8795 管理操作审计

`<8795-root>/control.sqlite3` 的 `admin_audit` 记录管理员 actor、action、target、变更前后 JSON
和时间。它只覆盖邀请码、额度、设置等控制面变更，没有自动清理，也不属于用户搜题 trace。
管理员登录成功/失败、普通邀请码登录、反馈审核/归档/删除目前不进入该表；反馈管理动作只改
反馈库。因此“admin audit”也不是 8795 所有操作的完整审计日志。

### 9. 结构化 Trace 事件（2.3）

| 项目 | 当前事实 |
| --- | --- |
| 生成器 | `TraceEventRecorder` → `SQLiteTraceEventStore` |
| 位置 | 各 8790/8896 运行根的 `trace_events.sqlite3` |
| 触发 | ingress、路由/阶段、模型/工具、费用提交、最终公共响应、反馈和请求失败 |
| 关联 | 服务端 `trace_id` 贯通 request、父 workflow、子 search/unit、run/call、`response_id` 和 `feedback_id` |
| 内容边界 | 事件类型白名单和 safe attributes 白名单；不存原文、Prompt、路径、URL 或异常正文 |
| 写失败 | 请求线程只做有界入队并 fail-open；丢失、校验、写入和重复终态计数由 `/health` 暴露 |

每个 trace 只有一个权威 terminal event，JSON、stream 和媒体后处理都以实际交付的最终协议为准。
Trace Store 独立于 8795，后者不是数据所有者或运行依赖。

### 10. 权威 Response 与反馈绑定（2.4）

| 项目 | 当前事实 |
| --- | --- |
| 生成器 | `SQLiteResponseStore.finalize`，默认由 Web app 在 feedback DB 同目录装配 |
| 位置 | 各 8790/8896 运行根的 `responses.sqlite3` |
| 触发 | JSON/stream、A3 父回复、A2 子题回复和可评分服务端错误完成最终协议/媒体投影后、公开前 |
| 主要字段 | `response_id/trace_id`、identity/session、request/workflow/search/unit、协议、phase/revision、章节/route、计数、耗时、创建/过期时间 |
| 内容边界 | 只存严格白名单投影；不保存题目正文、回复正文、完整对话、Prompt、路径或 URL |
| 一致性 | 同 trace 同投影幂等；同 trace 不同投影冲突；投影无法落盘时不暴露未绑定的可评分成功；stream 结果公开前断开会回滚或删除尚未公开的 response row |

反馈 schema v8 以唯一 `rated_response_id` 绑定 response；identity/session/有效期和 conversation
目标一致性均由服务端校验，协议与父子任务字段取自 response row。旧 v7 反馈迁移后
`rated_response_id=''`，仅作历史只读兼容。8795 可以展示该绑定，但不是 Response Store 所有者。

### 11. 非当前生产主链日志

`triage_shadow.jsonl`、`a3_decomposition.jsonl`、`a3_region_map.jsonl`、评测 records 和旧飞书
章节失败日志属于影子、试验或 8788 路径。除非对应启动入口明确装配，不能与 8790/8896
当前生产数据混合统计。

## 活跃数据与历史残留

启动代码与 2026-08-25 文件元信息共同证明：

| 路径 | 判断 | 理由 |
| --- | --- | --- |
| 8790 `a2/task_logs.jsonl` | 当前活跃 | 当前 A2 装配路径，修改时间与近期请求一致 |
| 8790 根 `task_logs.jsonl` | 历史残留 | 当前装配不写此处，修改早于当前 A3/A2 运行 |
| 8790 根 `model_costs.sqlite3` | 当前活跃共享账本 | 8790 启动复用 8896 builder，同一 ledger 同时注入父 A3 和子 A2 |
| 8790 `a2/model_costs.sqlite3` | 历史兼容库 | 当前 A2 接收外部共享 ledger；后台仍只读兼容旧数据 |
| 8790 根 `a3_sessions.sqlite3` | 当前活跃 | 当前 A3 store |
| 8790 `a2/session.db` | 当前活跃状态库 | 当前子 A2 session store |
| 8790 根 `session.db` | 历史残留 | 当前子 A2 store 在 `a2/session.db` |
| 8790 根 `trace_events.sqlite3` | 当前代码装配的权威事件库 | 2.3 store；与 8896 隔离，不依赖 8795 |
| 8790 根 `responses.sqlite3` | 当前代码装配的权威响应库 | 2.4 store；按需创建，每 trace 一条白名单投影 |
| 8790 `feedback.sqlite3`、`feedback_cases/` | 当前反馈源 | 8790 写入，8795 直接读写反馈管理字段/案例；元信息显示已有 5 个 case 目录和 22 个媒体文件 |
| 8790 根 stdout/stderr、status/PID | 当前服务文件 | 当前看门狗重定向/维护；无轮转，PID 可能陈旧 |
| 8790 `server*.log`、`manual_restart.*` | 历史启动残留 | 当前看门狗装配不使用这些文件 |
| 8896 根 `model_costs.sqlite3`、`a3_sessions.sqlite3` | 当前活跃 | 当前共享费用与 A3 store |
| 8896 根 `trace_events.sqlite3`、`responses.sqlite3` | 当前代码装配 | 与 8790 隔离的 2.3/2.4 store；response 按需创建 |
| 8896 `a2/session.db` | 当前活跃状态库 | 当前子 A2 session store |
| 8896 `a2/model_costs.sqlite3` | 历史残留 | 当前 A2 写根共享费用库 |
| 8896 `feedback.sqlite3` | 按需创建、当前尚不存在 | 入口已装配 feedback store，但没有写入就不会创建数据库 |
| 8896 `service_logs` 当前 stdout/stderr、status/PID | 当前服务文件 | 当前看门狗重定向/维护 |
| 8896 `service_logs/manual_restart.*`、`diagnostics/` | 手工/试验残留 | 当前服务装配不引用 |
| 8790/8896 `output_watchdog.jsonl` | 当前活跃 | 当前启动默认启用，元信息与近期请求一致 |
| 8795 `control.sqlite3` | 当前活跃共享控制/审计库 | 8795 写控制面，8790 读取并更新部分使用状态 |
| 8795 `service_logs` 当前 stdout/stderr、status/PID | 当前服务文件 | 当前后台看门狗重定向/维护 |

运行代码不得依靠文件修改时间选择数据源；上述“当前”应由启动装配和显式配置决定，文件
元信息只作为本次审计的旁证。

## 记录在哪里被看见

| 记录 | 当前查看位置 | 当前限制 |
| --- | --- | --- |
| 用户反馈入口 | 8790/8896 浏览器中带服务端 `response_id` 的助手回复 | API 强制 conversation，且目标 message 必须携带相同 response ID；旧消息/客户端错误不显示反馈按钮 |
| 反馈列表、详情和案例媒体 | 8795 管理后台直接读取 8790 feedback DB/cases | 不读取 8896 反馈；归档项的详情/媒体受限 |
| 反馈关联费用 | 8795 详情页读取 8790 根费用库和 A2 历史费用库 | 启发式 join，不是 trace/FK |
| 模型费用汇总 | 8795 总览、用户用量和反馈详情 | 读取根费用库及 A2 历史库；不可读时可能显示不完整/零值 |
| 管理审计 | 8795 设置页“最近操作” | 只覆盖部分控制面操作，不含登录和反馈管理 |
| A2 task、A3 page error、stdout/stderr、输出观察 | 只能由运维直接查看文件/SQLite | 8795 不聚合这些数据，没有一页式请求时间线 |
| Trace/Response | 各运行根 `trace_events.sqlite3`、`responses.sqlite3` | 已有权威 join；2.5 尚未提供稳定的 Codex/Agent 查询 CLI，8795 也不是数据所有者 |
| 看门狗状态 | 各 runtime root 的 status/PID 文件 | 只看进程代次，不看业务请求 |

所以目前“用户反馈对应哪版服务端回复”已经由 `rated_response_id` 权威确定，也能回到原响应
trace；但“前面发生了什么错误、哪次模型调用收费”仍缺一个稳定、有界的一站式查询入口。
这正是 2.5 的工作，不能把 8795 当前页面误当成长期诊断架构。

## 公共结果与错误词表

`PROTOCOL_REASONS` 当前共 100 个注册码：

| 状态 | 数量 | 含义 |
| --- | ---: | --- |
| `SUCCESS` | 19 | 请求或阶段成功 |
| `NO_MATCH` | 8 | 正常完成但未找到结果 |
| `NEEDS_INPUT` | 37 | 需要补充、重新登录、改章节或重试输入 |
| `PARTIAL` | 13 | 有结果但部分能力降级/不完整 |
| `ERROR` | 23 | 当前请求失败 |

按注册协议层统计为：tool 65、session 12、upload 5、media 5、login 3、quota 3、
feedback 3、queue 2、network 2。这个分布描述词表，不代表生产 emitter 覆盖率。

23 个注册 `ERROR` 码按层分组：

| 层 | 数量 | 注册码 |
| --- | ---: | --- |
| tool | 15 | `SERVICE_UNAVAILABLE`、`TOOL_FAILED`、`BANK_ROUTE_FAILED`、`GLOBAL_SEARCH_UNSUPPORTED_ROUTE`、`CANDIDATE_ACTION_INVALID_STATE`、`IMAGE_ANALYSIS_FAILED`、`MULTI_DETAIL_INVALID`、`MULTI_DETAIL_FAILED`、`MULTI_DETECTION_FAILED`、`COARSE_SEARCH_FAILED`、`GLOBAL_SEARCH_FAILED`、`RERANK_FAILED`、`ANSWER_LOOKUP_FAILED`、`AGENT_FAILED`、`AGENT_FAILED_NO_IMAGE` |
| queue | 2 | `QUEUE_FULL`、`QUEUE_TIMEOUT` |
| upload | 1 | `UPLOAD_PERSIST_FAILED` |
| network | 2 | `NETWORK_UNAVAILABLE`、`REQUEST_TIMEOUT` |
| media | 2 | `MEDIA_NOT_FOUND`、`MEDIA_ANSWERS_UNAVAILABLE` |
| feedback | 1 | `FEEDBACK_SAVE_FAILED` |

这只是公共词表。源码中注册或渲染了某个码，不足以证明当前生产路径一定会发出它。例如
反馈 SQLite 的非 `ValueError` 保存异常目前会落到全局未捕获异常处理，而不是明确构造
`FEEDBACK_SAVE_FAILED`；这类“已注册但缺少明确 emitter”的情况要在实现阶段补测试，不能
靠词表推断覆盖。

词表、服务端 emitter 和浏览器本地协议目前还不完全一致：

- `FEEDBACK_SAVE_FAILED`、`LOGIN_EXPIRED`、`MEDIA_PERSIST_FAILED`、
  `MULTI_DETECTION_FAILED`、`SESSION_EXPIRED` 虽已注册，但未找到清晰的当前生产 emitter；
- `INVITE_INVALID` 在 `_http_error_protocol` 有 401 映射，但当前邀请码无效路径直接返回 HTML，
  未找到实际产生该结构化协议码的路径；
- 服务端会直接返回未注册于 `PROTOCOL_REASONS` 的 `FEEDBACK_RECORDED`、
  `FEEDBACK_REMOVED`、`SESSION_RESET`；
- 浏览器还会本地构造未注册的 `RESPONSE_INVALID`，并使用不在服务端 `RequestAction` 枚举中的
  `retry_connection`。
- `NETWORK_UNAVAILABLE`、`REQUEST_TIMEOUT` 也只由浏览器生成；服务端没有对应 terminal
  事件。可能出现服务器已经执行并计费、浏览器却显示超时，而两侧无法精确对账。

因此后续实现和测试必须分别覆盖“注册词表”“实际 emitter”“浏览器本地协议”，不能假定
其中任意一套自动代表另外两套。

## 错误生成、规范化、输出与落盘矩阵

| 来源 | 规范化 | 用户输出 | 当前落盘 | 已知缺口 |
| --- | --- | --- | --- | --- |
| HTTP JSON/字段/上传校验 | `_http_error_protocol` 按路径、状态和少量 detail 映射 | 安全固定文案和五态协议 | 有 session 且非 login 时写一条边界任务摘要；同时进输出观察 | 54 个 `HTTPException` 抛出点被折叠为少量码；无 session/login 不进任务摘要 |
| 未登录 API | `LOGIN_REQUIRED` | 401 结构化 JSON | 进输出观察，不进任务摘要 | 没有用户 session，无法关联业务任务 |
| 未登录页面 | 不生成结构化协议 | 跳转登录页 | 不进 output watchdog/task log，只有服务访问/stdio 旁证 | 与 API 认证出口不同 |
| 无效邀请码表单 | 当前直接返回 HTML 401 | 登录页错误提示 | 不进 output watchdog/task log，只有服务访问/stdio 旁证 | `INVITE_INVALID` 映射存在但实际 emitter 不明确 |
| A2 队列与额度 | `_admit` 捕获 `AgentProtocolError` 子类并绑定 request/search | 429/503 固定文案 | `_admit` 写任务摘要 | 只记录最终阻断，不记录排队开始/等待过程 |
| 纯 A3 额度 | Web/A3 边界直接执行额度检查 | 固定额度文案 | 同步公共错误可进输出观察 | 通常不进入 A2 task log，也没有排队/等待事件 |
| A2 上传持久化 | 转 `UPLOAD_PERSIST_FAILED` | 固定重传文案 | `_admit` 写任务摘要 | 包装后通常只剩协议错误，原始文件系统异常不保留 |
| 纯 A3 上传持久化 | `persist_image` 异常落入通用边界 | `SERVICE_UNAVAILABLE` | 输出观察，未捕获时进 stderr | 不发 `UPLOAD_PERSIST_FAILED`，与 A2 同类失败语义不一致 |
| ToolResult error/partial/no-match | Agent renderer 与注册表产生固定反馈 | 固定文案/恢复动作 | turn 结束写任务摘要 | 工具内部 safe facts、重试和子步骤不在任务摘要；11 个通用异常边界会压缩具体错误 |
| A3 模型/解析/框选 | A3 阶段转固定协议或 `SERVICE_UNAVAILABLE` | 安全文案或人工回退 | 费用 run、结构化 stage/model/terminal event；部分路径另写 `a3_page_errors` | page-error 旧表仍非全覆盖，但 trace 已保留 ingress 与显式父子/unit 维度 |
| 媒体持久化/交付 | `_persist_response_media` 生成 media status/协议并可重开 A3 unit | 0/部分/全部交付文案 | warning/输出观察；Response Store 和 terminal event 记录后处理后的最终投影 | 旧 task 摘要仍可能显示媒体处理前的业务成功，查询时应以 terminal/response 为准 |
| 流式执行 | `_stream_agent_events` 捕获协议和通用异常 | NDJSON result/error | runtime task/stderr；流式实际交付写唯一 terminal event，可评分终态写 response row | 浏览器本地断网/超时仍不能证明服务端是否已完成或计费 |
| 反馈校验 | HTTP 400/413 → `FEEDBACK_INVALID/TOO_LARGE` | 固定文案 | feedback 边界任务摘要、输出观察和失败 terminal event | 多个具体校验原因仍折叠为公共安全码 |
| 反馈存储异常 | 当前一般落入全局未捕获异常 | `SERVICE_UNAVAILABLE` | stderr、边界摘要和请求失败 terminal event | 注册的 `FEEDBACK_SAVE_FAILED` 尚无稳定独立 emitter，DB/媒体/schema 仍被合并 |
| 未捕获 FastAPI 异常 | 全局转 `SERVICE_UNAVAILABLE` | 固定 500 文案 | stderr、边界摘要、输出观察和安全 terminal event | traceback 仍只在非结构化 stderr；trace 不复制异常正文 |
| 观测/任务/费用写入异常 | 旧 sink 多数 fail-open | 不影响用户 | Trace writer 有健康计数；旧 task/cost/page-error/output sink 各自规则不变 | 新健康计数不能补记旧 sink 已丢数据，2.5 查询必须显示证据缺失 |

## 重复、漏记与语义冲突

### 可能重复

1. `_run` 内部异常会在 finally 先写一条 turn 摘要；若异常继续到 `_admit` 或 FastAPI
   全局 handler，可能再写一条同 request 的边界摘要。
2. A3 schema 每次失败尝试各写一条 `a3_page_errors`，重试耗尽后还会再写 terminal 行；这是
   有意的“attempt + terminal”多记录，后续不能按相同 search/time 盲目去重，必须显式标明类型。
3. 纯 A3 已捕获的页面理解失败通常落在费用、`a3_page_errors` 和输出观察，不会自然进入
   A2 task log，也通常不进 stderr；不同入口的 sink 组合不一致。
4. 反馈保存成功会写 `feedback.sqlite3`，随后写 `kind=feedback` 任务摘要和结构化 trace event；
   它们是不同视图，但新事件已携带 `feedback_id/rated_response_id`，可经 response row 回到原 trace。
5. 媒体持久化发生在 A2 turn 已写任务摘要之后；task JSONL 可能记录业务 `SUCCESS`，随后 Web
   把最终协议改成媒体降级。2.3/2.4 已让 terminal event/response row 反映实际交付，但 2.5 查询
   必须区分“业务结果”和“交付结果”，不能盲信旧 task 摘要。

### 确定漏记或可静默丢失

1. task JSONL 写失败被吞；
2. cost SQLite 写失败被吞，已发生的调用和费用可能没有账；
3. A3 page error 写失败被吞；
4. output watchdog 队列满、目录失败或写失败均被吞，没有 dropped counter；
5. 无 session 的 login/部分 HTTP 错误不进任务摘要；
6. runtime 外的流式异常可能只有 stderr；
7. stdout/stderr、task JSONL、output JSONL、费用和 admin audit 都没有统一滚动/清理策略；
8. A3 page error 仅覆盖 page-understanding schema/terminal 及 auto-grounding 等部分路径；crop
   verify、外部载入、单题自动校验、裁图写入和 overlay 等失败可能只留在状态/费用/stderr，
   或被降级处理而没有 page-error 记录；
9. 浏览器本地的网络、超时和响应格式错误不进入任何服务端存储。

费用查询还有一个可观测性语义风险：部分 SQLite `OperationalError` 被兼容性查询当成 0 或跳过，
“账本不可读”在后台可能表现成“没有消费”，而不是明确的账本健康错误。

### ID 语义冲突

- 历史应用 `request_id` 与模型供应商 `request_id` 同名；新费用行以
  `provider_request_id` 为权威并仅镜像旧列；
- 纯 A3 `_response()` 会另建 request ID，部分直接响应又不带 ID；body 可能与
  `X-Request-ID` 不同或为空；
- 历史 A2/A3 `run_id` 语义不一；新 run 使用独立 `run_...`；
- A3 `current_search_id` 在路由过程中可能代表父 workflow 或子搜索；
- `unit_id` 只在一个 workflow 内唯一；
- 反馈 UI 的 `message_id` 仍由客户端创建，但权威被评分协议和父子任务字段来自服务端 response row；
  `message_id` 只能与同一 `rated_response_id` 配对，不能重绑。

这些冲突是新增独立 `trace_id/response_id/provider_request_id`，而不是继续复用旧字段的直接理由。

## 反馈全链路

### 提交与校验

1. 服务器在最终安全回复/媒体载荷形成后保存白名单投影并返回 `response_id`；浏览器把它与自行
   创建的 `message_id` 一起保存。没有 response ID 的旧消息和客户端本地错误不可评分。
2. 用户评分时，浏览器提交 rating、tags、detail、`rated_response_id` 和截至目标消息的
   conversation；目标消息本身也携带相同 response ID。
3. `/api/feedback` 限制请求大小、字段类型、标签、detail、conversation、`message_id` 和
   `rated_response_id` 格式；conversation 缺失直接拒绝。
4. 服务端用邀请码身份和 session cookie 得到 `identity_key/session_key`，再从 Response Store
   查找未过期且完全同 owner 的 response；不存在、过期、跨用户或跨 session 均按未找到处理。
5. 服务端要求 conversation 中存在目标 `message_id`，且目标消息的 response ID 与
   `rated_response_id` 完全一致；任一 ID 已绑定另一方时拒绝更新，消除 message/response 重绑。
6. 协议、revision、候选数、章节、image route、父 workflow、子 search 和 intent 均从服务端
   response 投影取值，不再信任客户端历史或提交时最新 snapshot。反馈范围仍由服务端据此分类为
   `page|question`；conversation 只作为受限案例证据，不是关联权威。

### 数据库存储

8790 使用 `<8790-root>/feedback.sqlite3` 的 `message_feedback`；当前代码 schema 为 v8：

- 服务端字段：`feedback_id/feedback_number/rated_response_id`、identity/session、review/archive、
  创建/更新时间；
- 用户评价：rating、tags、detail；
- response 投影字段：revision、candidate count、duration、phase、chapter、image route、
  request/search/workflow、status/layer/code、intent 和 scope；
- 浏览器辅助字段：`message_id` 和受限 conversation，仅用于 UI/案例证据；
- 证据：裁剪后的 conversation JSON、过期/清理时间。

新记录同时受 `(identity_key, session_key, message_id)` 和非空 `rated_response_id` 唯一约束；同一
response/消息再次评分只更新原记录，不能改绑。删除反馈也按 response ID 加 owner 校验。

2026-08-25 的只读元信息基线显示：当时 8790 数据库为 schema v7、共 5 行，案例目录 5 个、媒体文件
22 个（约 2.53 MB）；只有 2/5 行同时具备 request/search/workflow 关联字段。未读取反馈正文或
媒体内容。v8 代码首次打开旧库时加法迁移，历史行保持 `rated_response_id=''`，后台明确标为
legacy binding；不会根据旧客户端字段伪造 response 归属。

### 对话和媒体证据

- 只保留最近一次相关用户输入到目标回复，不保存目标之后的消息；
- conversation 最多 50 条、单条文本最多 5000 字符、总文本最多 60000 字符；
- 每条最多复制 8 个 session-owned 图片，并可复制 A3 overlay；
- 证据复制到 `<8790-root>/feedback_cases/<feedback_id>/`，数据库只存随机文件名；
- 删除用户反馈时同时删除 case 目录；管理员归档只改变状态，不自动删除记录。

证据存储还有三个独立于 trace 的完整性风险：

- 文件复制与 SQLite upsert 不在同一事务；更新时先删旧目录，中途失败可能丢旧证据或留下孤儿文件；
- 只限制 JSON 请求大小和单条图片数量，没有总媒体字节上限或内容去重，重复引用大文件会放大磁盘占用；
- 8790 写案例与 8795 清理/删除运行在两个进程，现有 Python `Lock` 不能提供跨进程互斥。

### 证据过期

- store 支持由 provider 提供 1～365 天保留期；但当前 8790 和 8896 启动均未传
  `feedback_retention_days_provider`，新案例实际固定使用默认 30 天，8795 控制库设置没有接到
  两个 Web 写入路径；
- 过期清理会删除 case 媒体、清空 conversation，并保留评分元数据和 `case_purged_at`；
- 当前只有 8795 FastAPI lifespan 启动时调用 `purge_expired_cases()`，没有周期任务；若 8795
  长期不重启，已过期证据可能继续留盘超过设定日期。

### 8795 查询和费用关联

8795 直接读取 8790 feedback DB/cases，并同时查询根共享费用库和 A2 历史费用库：

- v8 反馈列表/详情展示 `rated_response_id`，旧 v7 行明确标记为 legacy binding；

- page 反馈用 `workflow_search_id` 找费用；
- question 反馈用 `search_key` 和 `workflow_search_id`；
- 再按 `identity_key` 过滤；
- 客户端目标 `createdAt` 只有在不晚于提交时间且滞后不超过 30 分钟时才作为 run 上界，
  否则退回服务端反馈创建时间；
- 根费用库先读、A2 历史库后读，并跨库按 `run_id` 去重；后读库的同 ID run 会被丢弃；
- 浏览器时钟偏差过大时退回服务器提交时间。

权威 response 绑定没有自动把旧费用查询变成外键查询。当前费用展示仍是兼容性启发式关联：
同一 search key 的多次 run、客户端时钟、历史字段缺失
都可能使展示费用包含多次调用或需要 fallback。该 join 不使用 `feedback_id/message_id`、
被评分回复的 `request_id`、`session_key/task_revision` 或明确的 `run_id`；任一费用库读取失败还会
fail-open 跳过，后台可能显示不完整或零费用。

### 信任边界

| 字段/行为 | 当前信任来源 | 结论 |
| --- | --- | --- |
| identity | 经验证的邀请码 Cookie | 生产身份权威 |
| session | 非空客户端 session Cookie 的哈希 | 不暴露 Cookie；反馈必须与 response row 的 session owner 完全一致 |
| response ID/投影 | 服务端最终安全载荷写入 Response Store | 被评分回复权威；只存白名单元数据，不存回复正文 |
| feedback ID/number | 服务端生成 | 权威 |
| scope | 服务端根据 response intent/route 分类 | 服务端权威投影派生，不依赖客户端声明 |
| 媒体文件 | 只允许当前/保留 session 的安全 resolver | 服务端所有权校验 |
| message ID | 浏览器生成后回传 | 非权威 UI ID；必须在 conversation 中存在并与同一 `rated_response_id` 一一绑定 |
| rated protocol/request/search | 服务端 response 投影 | 精确对应目标回复；不采用客户端覆盖或最新 snapshot |
| conversation/目标时间 | 浏览器回传后裁剪和限长 | 强制提交且目标需匹配，但内容仍只是受限案例证据，不是响应权威 |
| 费用 | 服务端本地账本 | 行由服务端生成但可能漏写且金额是估算；反馈到费用的 join 是启发式 |

## 2.1 设计输入与当前完成状态

下列约束来自 2.1 基线，继续约束后续实现：

1. `trace_id` 由服务器生成，每个 HTTP 操作唯一；现有公共 `request_id` 只保留兼容。
2. 新事件必须明确 `event_type/stage`，避免把内部 turn、边界错误和同一异常的不同视图当重复事故。
3. 模型 call 新字段使用 `provider_request_id`，历史 `request_id` 只读兼容。
4. 明确 `workflow_search_id/search_id/unit_id` 父子关系，禁止依赖 `current_search_id` 猜语义。
5. 最终公共回复必须有服务端 `response_id`；反馈用 `rated_response_id` 绑定，不再以浏览器
   `message_id` 作为唯一事实。
6. trace writer 仍可 fail-open，但必须有 dropped/write-failure 计数和进程级健康出口；不能继续
   `except: pass` 后完全无证据。
7. 新 trace 不替换现有 task/cost/feedback 数据；先加法双写、对照一致，再迁移后台查询。
8. 为日志/输出观察/trace metadata 定义大小、滚动和保留策略；内容/媒体过期不能被 trace 延长。
9. 反馈证据清理应由周期任务或明确维护命令驱动，不能只依赖 8795 重启。
10. 服务端必须保存可验证的响应记录；不能让“conversation 可省略”继续绕过反馈目标绑定。
11. 下一阶段测试必须覆盖：正常 A2、A3 多题父子、准入失败、内部异常、媒体失败、同题重试、
    旧回复反馈、writer 失败、重复事件抑制、协议词表/emitter/浏览器一致性和隐私白名单。

其中 1～7 的 trace/response 主链及第 10 项 optional-conversation 漏洞已在 2.2～2.4 完成；
`rated_response_id`、owner/expiry、目标 message 一致性和旧 v7 兼容均有定向回归。第 8～9 项的
统一保留/周期清理，以及把这些物理 store 变成稳定的 Codex/Agent 有界查询入口，属于 2.5。
8795 最多是该入口未来的只读消费者，不是完成门或数据所有者。

## 本阶段完成门槛

第一阶段只有同时满足以下条件才算完成：

- 当前活跃与历史日志路径已区分；
- 每类记录的生成器、触发、字段、保留和 fail-open 条件已列出；
- 公共错误词表与实际异常漏斗已区分；
- 注册词表、服务端 emitter 与浏览器本地协议的不一致已列出；
- 重复记录、静默丢失和非结构化 stderr 风险已列出；
- 反馈入口、当时存在的可选 conversation 绕过、DB、case 媒体、过期、后台查询、费用 join 和信任边界已核对；该绕过现已由 2.4 关闭；
- 最小 Trace V1 契约吸收上述约束；
- 未读取或写入用户日志正文、反馈正文、媒体和敏感配置。
