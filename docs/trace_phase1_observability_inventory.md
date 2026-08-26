# Trace Phase 1 可观测性现状盘点

状态：2.1 实施前审计快照（保留作为基线）；2.2 Trace Context 与 2.3 结构化事件/终态已完成

日期：2026-08-25

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
- `tiku_agent/demo_web/demo.js`、`tiku_agent/feedback_store.py`、
  `tiku_admin/reporting.py`：反馈提交、存储、证据过期和费用关联；
- 相关协议、运行时、A3、FastAPI、输出观察、费用和后台测试。

静态代码可以证明错误出口和落盘规则，但不能穷举第三方库、操作系统、SQLite、网络和
模型服务未来返回的所有异常文本。本文件区分“注册的公共结果码”“实际错误漏斗”和
“无法穷举的底层异常”，不把它们混为一谈。

## 核心结论

1. 当前不是一套日志，而是八套独立记录：A2 任务 JSONL、模型费用 SQLite、A3 页面错误
   SQLite、公共输出观察 JSONL、进程 stdout/stderr、看门狗状态、反馈，以及 8795 管理审计。
2. 8790 当前任务摘要写在 `a2/task_logs.jsonl`，不是根目录旧的
   `task_logs.jsonl`；当前共享费用写根目录 `model_costs.sqlite3`，不是旧的
   `a2/model_costs.sqlite3`。仅凭文件名很容易读错数据源。
3. 公共协议注册 100 个结果码，其中 23 个状态为 `ERROR`；但“注册”不等于当前每个
   码都有生产发出点，用户可见失败也可能是 `NEEDS_INPUT`、`NO_MATCH` 或 `PARTIAL`。
4. 有些异常会产生两条任务记录，有些只进 stderr，有些观测/费用/日志写入失败会被
   fail-open 静默吞掉，因此当前既存在重复也存在盲区。
5. 反馈数据库和证据位置明确，但目标 `message_id`、对话和被评分协议来自浏览器；
   服务端没有权威的最终响应记录，所以还不能精确证明“这条反馈对应服务器发出的哪一版回复”。
6. 当前存储没有统一 `trace_id`，不能用一个键无歧义地串起路由、A3/A2、工具、模型、
   费用、公共回复和反馈。

## 运行根目录与职责

| 名称 | 默认路径 | 职责 |
| --- | --- | --- |
| 8790 root | `.tmp_tiku_agent_v2_prod_8790` | 生产 A3 父流程、共享费用、反馈、输出观察和服务日志 |
| 8790 A2 root | `.tmp_tiku_agent_v2_prod_8790/a2` | 当前子题 A2 会话、题图/媒体和任务摘要 |
| 8896 root | `.tmp_tiku_agent_a3_mvp_8896` | 隔离验收 A3、共享费用和输出观察 |
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
| 主要字段 | `task_id/request_id/search_id/session_key/identity_key`、kind、起止时间、阶段前后、outcome、题数、候选数、章节、route、`error_kind`、五态协议字段 |
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
| run 字段 | run/session/identity/search/task kind、起止时间、outcome、调用数、token、估算费用和 warning |
| call 字段 | call/run/sequence、provider/model/call type/status、延迟、token、尝试数、`request_id`、`error_kind` 和价格 |
| 语义冲突 | call 表的 `request_id` 是模型供应商响应 ID；A2 run 常把应用请求 ID 当 `run_id`，A3 run 又用 `new_request_id()` 生成独立值 |
| 保留 | 没有自动删除或滚动 |
| 写失败 | A2/A3 都捕获并静默继续；可能已经产生模型费用但账本没有对应 run |

旧的 `<root>/a2/model_costs.sqlite3` 仍可能存在，但当前 8790/8896 装配把同一个根目录共享
账本注入父 A3 与子 A2。后台为兼容历史会同时读取根库和 A2 旧库。

A2 还把客户端可复用的 HTTP `request_id` 用作 cost `run_id`，而 ledger 对 run 使用
`INSERT OR REPLACE`。若客户端复用 request ID，新 run 行可能覆盖旧 run 行，旧 call 行却继续
留在同一 run ID 下，造成新旧调用混合。这不仅是“关联不方便”，还可能改变账本语义。

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
| 内容风险 | error message 最长 500 字符，保留异常链中的具体消息；没有统一字段白名单或 trace |

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

### 9. 非当前生产主链日志

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
| 8790 `feedback.sqlite3`、`feedback_cases/` | 当前反馈源 | 8790 写入，8795 直接读写反馈管理字段/案例；元信息显示已有 5 个 case 目录和 22 个媒体文件 |
| 8790 根 stdout/stderr、status/PID | 当前服务文件 | 当前看门狗重定向/维护；无轮转，PID 可能陈旧 |
| 8790 `server*.log`、`manual_restart.*` | 历史启动残留 | 当前看门狗装配不使用这些文件 |
| 8896 根 `model_costs.sqlite3`、`a3_sessions.sqlite3` | 当前活跃 | 当前共享费用与 A3 store |
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
| 用户反馈入口 | 8790/8896 浏览器中每条可反馈的助手回复 | 正常 UI 会带 conversation，但 API 本身允许省略 |
| 反馈列表、详情和案例媒体 | 8795 管理后台直接读取 8790 feedback DB/cases | 不读取 8896 反馈；归档项的详情/媒体受限 |
| 反馈关联费用 | 8795 详情页读取 8790 根费用库和 A2 历史费用库 | 启发式 join，不是 trace/FK |
| 模型费用汇总 | 8795 总览、用户用量和反馈详情 | 读取根费用库及 A2 历史库；不可读时可能显示不完整/零值 |
| 管理审计 | 8795 设置页“最近操作” | 只覆盖部分控制面操作，不含登录和反馈管理 |
| A2 task、A3 page error、stdout/stderr、输出观察 | 只能由运维直接查看文件/SQLite | 8795 不聚合这些数据，没有一页式请求时间线 |
| 看门狗状态 | 各 runtime root 的 status/PID 文件 | 只看进程代次，不看业务请求 |

所以目前“用户反馈在哪里”是明确的：写入对应 Web root，生产反馈由 8795 查看；但“这条反馈
前面发生了什么错误、哪次模型调用收费、用户最终收到哪版回复”没有一个权威页面或统一 join。

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
| A3 模型/解析/框选 | A3 阶段转固定协议或 `SERVICE_UNAVAILABLE` | 安全文案或人工回退 | 费用 run；部分路径写 `a3_page_errors`；边界可能再写任务摘要 | 不是所有 A3 异常都写 page error；unit 与 ingress request 未贯通 |
| 媒体持久化/交付 | `_persist_response_media` 生成 media status/协议并可重开 A3 unit | 0/部分/全部交付文案 | warning 进 stderr，最终文案进输出观察 | 媒体后处理发生在 Agent turn 日志之后，任务摘要可能仍显示原业务成功 |
| 流式执行 | `_stream_agent_events` 单独捕获队列/额度，其他异常走通用分支 | NDJSON error 事件 | runtime 内异常可能已有任务摘要；通用异常 `logger.exception` 进 stderr | 普通 `AgentProtocolError` 可能被压成 `SERVICE_UNAVAILABLE`；流式 terminal error 不进输出观察，也不新增协议事件记录 |
| 反馈校验 | HTTP 400/413 → `FEEDBACK_INVALID/TOO_LARGE` | 固定文案 | 有 session 时写 feedback 边界任务摘要，进输出观察 | 多个不同校验原因被折叠；不保存具体安全原因码 |
| 反馈存储异常 | 当前一般落入全局未捕获异常 | `SERVICE_UNAVAILABLE` | stderr + 全局边界任务摘要（若有 session） | 注册的 `FEEDBACK_SAVE_FAILED` 未形成稳定 emitter；无法区分 DB、媒体复制或 schema 问题 |
| 未捕获 FastAPI 异常 | 全局转 `SERVICE_UNAVAILABLE` | 固定 500 文案 | `logger.exception` → stderr；有 session 时写边界任务摘要；进输出观察 | traceback 非结构化，且与请求仅靠时间/request ID 旁证 |
| 观测/任务/费用写入异常 | 多数直接 `except Exception: pass` | 不影响用户 | 通常无第二落点 | 真正的可观测性盲区：写失败本身不可观测 |

## 重复、漏记与语义冲突

### 可能重复

1. `_run` 内部异常会在 finally 先写一条 turn 摘要；若异常继续到 `_admit` 或 FastAPI
   全局 handler，可能再写一条同 request 的边界摘要。
2. A3 schema 每次失败尝试各写一条 `a3_page_errors`，重试耗尽后还会再写 terminal 行；这是
   有意的“attempt + terminal”多记录，后续不能按相同 search/time 盲目去重，必须显式标明类型。
3. 纯 A3 已捕获的页面理解失败通常落在费用、`a3_page_errors` 和输出观察，不会自然进入
   A2 task log，也通常不进 stderr；不同入口的 sink 组合不一致。
4. 反馈保存成功会写 `feedback.sqlite3`，随后再写一条 `kind=feedback` 的任务摘要；这是
   两种不同记录，但当前没有反馈 ID 将它们精确连接。
5. 媒体持久化发生在 A2 turn 已写任务摘要之后；task JSONL 可能记录业务 `SUCCESS`，随后 Web
   把最终协议改成 `MEDIA_CANDIDATES_INCOMPLETE`、`MEDIA_ANSWERS_PARTIAL` 或
   `MEDIA_ANSWERS_UNAVAILABLE`，形成“日志成功、用户失败/降级”的冲突。

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

- 应用 `request_id` 与模型供应商 `request_id` 同名；
- 纯 A3 `_response()` 会另建 request ID，部分直接响应又不带 ID；body 可能与
  `X-Request-ID` 不同或为空；
- A2 `run_id` 常等于请求 ID，A3 `run_id` 是另一个 `req_...`；
- A3 `current_search_id` 在路由过程中可能代表父 workflow 或子搜索；
- `unit_id` 只在一个 workflow 内唯一；
- 反馈 `message_id` 和被评分协议由客户端带回，服务端没有权威 response row。

这些冲突是新增独立 `trace_id/response_id/provider_request_id`，而不是继续复用旧字段的直接理由。

## 反馈全链路

### 提交与校验

1. 服务器返回安全回复和协议；浏览器渲染后自行创建 `message_id`。
2. 用户评分时，浏览器提交 rating、tags、detail、目标附近 conversation、耗时、协议字段、
   request ID 和 search ID。
3. `/api/feedback` 限制请求大小、字段类型、标签、detail、conversation 和 `message_id` 格式。
4. 正常网页会提交 conversation，此时服务端要求其中存在同 ID 目标，缺失就 fail-closed；但 API
   允许完全省略 conversation，省略后目标绑定校验不会执行，格式合法的任意 `message_id` 仍可写入。
5. 服务端用 session cookie 和邀请码身份确定 `session_key/identity_key`，并读取当前 snapshot
   作为缺失字段的 fallback。
6. 反馈范围由服务端根据目标 intent、A3 overlay 和 image route 判定为 `page|question`，不信任
   客户端直接声明的 scope；但分类所依赖的目标 intent/overlay 仍来自客户端回传，只能算受约束
   的客户端信号。

### 数据库存储

8790 使用 `<8790-root>/feedback.sqlite3` 的 `message_feedback`：

- 服务端字段：`feedback_id/feedback_number`、identity/session、review/archive、创建/更新时间；
- 用户评价：rating、tags、detail；
- 客户端目标优先字段：revision、candidate count（缺失时才退回当前 snapshot）；
- 客户端计算字段：搜索 duration；
- 提交时当前 snapshot：phase、chapter、image route，评价旧回复时可能已漂移；
- 关联字段：message/request/search/workflow search、status/layer/code、scope；
- 证据：裁剪后的 conversation JSON、过期/清理时间。

唯一约束是 `(identity_key, session_key, message_id)`；同一目标再次评分会更新原记录。

本次只读元信息核验显示：当前 8790 数据库为 schema v7、共 5 行，案例目录 5 个、媒体文件
22 个（约 2.53 MB）；只有 2/5 行同时具备 request/search/workflow 关联字段。未读取反馈正文或
媒体内容。当前没有已清理或已到期未清理的案例；这些数量只说明现状，不证明关联链路完整。

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

- page 反馈用 `workflow_search_id` 找费用；
- question 反馈用 `search_key` 和 `workflow_search_id`；
- 再按 `identity_key` 过滤；
- 客户端目标 `createdAt` 只有在不晚于提交时间且滞后不超过 30 分钟时才作为 run 上界，
  否则退回服务端反馈创建时间；
- 根费用库先读、A2 历史库后读，并跨库按 `run_id` 去重；后读库的同 ID run 会被丢弃；
- 浏览器时钟偏差过大时退回服务器提交时间。

这是兼容性启发式关联，不是外键：同一 search key 的多次 run、客户端时钟、历史字段缺失
都可能使展示费用包含多次调用或需要 fallback。该 join 不使用 `feedback_id/message_id`、
被评分回复的 `request_id`、`session_key/task_revision` 或明确的 `run_id`；任一费用库读取失败还会
fail-open 跳过，后台可能显示不完整或零费用。

### 信任边界

| 字段/行为 | 当前信任来源 | 结论 |
| --- | --- | --- |
| identity | 经验证的邀请码 Cookie | 生产身份权威 |
| session | 非空客户端 session Cookie 的哈希 | 不暴露 Cookie，但反馈入口不验证 session 当前有效或拥有目标回复 |
| feedback ID/number | 服务端生成 | 权威 |
| scope | 服务端根据目标消息与 route 分类 | 服务端计算，但依赖客户端目标内容，不能证明响应真实性 |
| 媒体文件 | 只允许当前/保留 session 的安全 resolver | 服务端所有权校验 |
| message ID | 浏览器生成后回传 | 非权威；始终只校验格式，只有提交了 conversation 时才校验其中存在目标 |
| rated protocol/request/search | 浏览器历史回传，缺失时 snapshot fallback | 客户端介导，无法证明对应精确响应 |
| conversation/目标时间 | 浏览器回传后裁剪和限长 | 内容受限但不是服务器原始响应记录 |
| 费用 | 服务端本地账本 | 行由服务端生成但可能漏写且金额是估算；反馈到费用的 join 是启发式 |

## 第一阶段确认的设计输入

进入 Trace Context 实现前，必须以本盘点为约束：

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

## 本阶段完成门槛

第一阶段只有同时满足以下条件才算完成：

- 当前活跃与历史日志路径已区分；
- 每类记录的生成器、触发、字段、保留和 fail-open 条件已列出；
- 公共错误词表与实际异常漏斗已区分；
- 注册词表、服务端 emitter 与浏览器本地协议的不一致已列出；
- 重复记录、静默丢失和非结构化 stderr 风险已列出；
- 反馈入口、可选 conversation 绕过、DB、case 媒体、过期、后台查询、费用 join 和信任边界已核对；
- 最小 Trace V1 契约吸收上述约束；
- 未读取或写入用户日志正文、反馈正文、媒体和敏感配置。
