# 题库 Agent 输出层升级：阶段 1 现状审计与基线

## 1. 审计结论

阶段 1 已确认：统一用户输出层是一个真实的可靠性缺口，但不应重写现有业务流程，也不应把所有回复交给模型。

当前系统已经具备三块可复用基础：

- `ToolResult` 提供 `SUCCESS / NO_MATCH / NEEDS_INPUT / PARTIAL / ERROR` 五态工具结果；
- `RequestProtocol` 提供跨接口的状态、层级、错误码、可重试性和主恢复动作；
- `tiku_agent/render.py`、`reply_shell_v2.py` 和 A3 固定回复已经包含大量正确、简洁的用户话术。

真正缺少的是最后一道强制边界。目前不是所有用户可见内容都从受控模板产生；部分工具说明、异常详情、HTTP `detail`、流式错误和前端兜底仍允许来源字符串直达用户。

因此后续最小正确方向是：

1. 保留已审核的固定话术；
2. 把动态事实和允许动作结构化；
3. 禁止原始异常、工具 `error`、HTTP `detail` 和模型原文默认进入用户回复；
4. 普通 JSON、流式接口和浏览器只负责传输/展示同一个语义结果；
5. 第一版不新增模型调用。

## 2. 本次审计范围

### 2.1 纳入范围

本轮以当前生产入口 `8790` 为权威范围：

```text
scripts/run_tiku_agent_8790.py
        ↓
scripts/run_tiku_agent_8896.py::build_runtime
        ↓
A3MvpRuntime ── 子流程 ── AgentSessionRuntime / TikuSearchAgent（A2）
        ↓
fastapi_demo.py（普通 JSON + 流式接口）
        ↓
demo_web/demo.js（浏览器展示和浏览器本地错误）
```

具体审计了：

- A2 成功、等待输入、无匹配、部分成功、失败和答案回复；
- A3 整页识别、选题、自动/人工裁剪、子 A2、取消、完成和异常回复；
- 闲聊/能力说明的 `safe_answer_v0` 与 `reply_shell_v2`；
- 图片预检 A1/A2/A3 结果说明；
- Session Runtime 的队列、额度、进度和协议异常；
- FastAPI 普通响应、HTTP 异常、流式 `progress/result/error`；
- 浏览器端的服务端错误映射、流式错误和本地图片/网络错误；
- 与上述路径相关的现有测试。

### 2.2 暂不纳入

- 旧飞书机器人 `8788`；
- 管理后台 `8795` 的管理员提示和表格文案；
- 题库 CLI 的终端输出；
- 输入总控大脑或新的闲聊能力；
- 检索、排序、章节、裁图、状态机和计费逻辑本身。

原因：本方案原始问题明确指向新题库 Agent 的 A2、A3、网页和流式出口。把旧飞书、后台和 CLI 同时重构会扩大风险，也不利于在 `8790` 隔离验证。若以后复用输出策略，应复用结构化结果和文案目录，而不是让旧入口直接依赖 `8790` 的会话实现。

## 3. 用户可见出口清单

下表是逻辑出口，不是每一条文案的逐字重复。一次源码导航统计显示，核心 Agent/A3/Runtime/Web 中约有 90 处响应构造或包装调用、15 处服务端进度调用、54 处 HTTP 异常抛出，以及 38 处前端错误或事件消息展示点。数字只用于说明出口分散程度，不作为实现验收指标。

| 层级 | 当前出口 | 当前事实来源 | 用户文本来源 | 审计结论 |
| --- | --- | --- | --- | --- |
| A2 固定业务回复 | `tiku_agent/render.py:11-208`，由 `agent.py` 调用 | `AgentState` | 固定函数/动态模板 | 主要应保留 |
| A2 零工具对话 | `safe_answer_reply_v0.py`、`reply_shell_v2.py` | 对话分类和受限状态事实 | 固定模板；部分 safe-answer 可用受约束模型 | 与业务输出层职责不同，但最终仍应过同一安全契约 |
| A2 工具停止边界 | `agent.py:998-1045` | `ToolResult` | `ToolResult.error` 或 `render_error()` | 存在高风险直通 |
| A2 部分降级提示 | `agent.py:1047-1058` | `ToolResult` | `error` / `rerank_note` 拼接 | 存在高风险直通 |
| A2 无匹配/答案缺失 | `agent.py:765-888` | 搜索、复筛、答案工具 | 部分固定，部分直接使用工具 `error` | 需要按 code 映射 |
| 图片预检回复 | `image_triage_authority.py:20-217` | 预检 handoff | 固定策略、固定兜底，或受约束模型回复 | 固定部分保留；模型部分需更强终检 |
| A3 状态回复 | `a3_runtime.py:418-1919` | `A3SessionState` 和状态机决定 | 大量固定文本和动态模板 | 主要应保留语义 |
| A3 子 A2 后置改写 | `a3_runtime.py:1923-1975` | 子 A2 响应 + A3 题号/剩余题数 | 原地修改 `response.text` | 有事实漂移和拼接风险 |
| Session Runtime | `session_runtime.py:102-160, 391-671, 864-1035` | 队列、额度、预检、运行协议 | 固定异常文字和 progress 回调文字 | 当前大多安全，但没有强制输出边界 |
| HTTP 普通错误 | `fastapi_demo.py:131-218, 957-1014` | `HTTPException` / Runtime 异常 | 多处 `str(exc)` / `str(exc.detail)` | 存在高风险直通 |
| 流式输出 | `fastapi_demo.py:1371-1410` | progress 回调、结果或异常 | progress 原样；协议异常 `str(exc)` | 与普通接口未共享文案权威 |
| 浏览器服务端错误 | `demo_web/demo.js:1214-1266` | HTTP/流式 payload | code 映射后仍可回退 `rawDetail` / `event.message` | 存在高风险直通和重复策略 |
| 浏览器本地错误 | `demo_web/demo.js:1175-1250, 1270-1368, 1537-2438` | 浏览器图片、网络、超时和 UI 状态 | 浏览器固定模板 | 必须保留本地能力，但应使用同一 code/动作语义 |

## 4. 现有话术基线分类

### 4.1 A 类：可冻结的固定话术

这些回复的事实由代码确定，表达简洁，且给出的动作与当前流程一致。后续接入输出层时应通过稳定的 `message_key` 原样保留，不做模型润色。

| 类别 | 代表实现/测试 | 冻结内容 |
| --- | --- | --- |
| 初始能力说明 | `render.py:11-17` | 品牌、能力边界、上传题图入口 |
| 章节不确定/不支持 | `render.py:19-61`；章节范围测试 | 诚实说明不确定或不支持，并列出允许范围 |
| 答案成功/重发 | `render.py:152-162`；答案选择测试 | 仅在答案路径存在时宣称已发送 |
| 取消 | `render.py:164-165` | 明确已取消，不附加未经允许的动作 |
| A3 裁剪未通过 | `a3_runtime.py:2447-2463`；`test_a3_runtime.py:961-1050` | 原因来自固定检查项，动作是重新裁剪/提交 |
| A3 范围澄清 | `a3_runtime.py:648-860`；A3 cancel/namespace tests | 不擅自取消，要求明确当前题或整页范围 |
| A3 当前题完成且仍有剩余题 | `a3_runtime.py:1923-1975`；`test_a3_runtime.py:950-959` | 题号和剩余题数来自状态机，并明确还能继续选择 |
| 无外荷载门禁 | `image_triage_authority.py:20-24`；`test_image_triage_authority.py:109-117` | 不进入检索，并要求补充完整荷载题图 |
| 未处理异常兜底 | `fastapi_demo.py:195-218` | 不泄露异常，只提示稍后重试 |
| 队列/额度 | `session_runtime.py:137-160, 1015-1035`；`test_tiku_agent_fastapi_demo.py:755-787` | 明确繁忙或额度限制，并匹配协议动作 |

### 4.2 B 类：冻结语义，不冻结当前拼接方式

以下回复包含合法动态事实，不能简单保存为一段固定字符串。阶段 2 必须为其定义白名单字段和模板：

- 当前章节名称；
- 原图稳定题号或题目显示标签；
- 题目数、候选数、已准备数、需人工裁剪数、剩余题数；
- 候选来源章节；
- 当前允许继续、换章节、全局搜、选候选、裁剪、取消当前题或结束整页等动作；
- 部分降级是否仍返回了可用候选。

此外，当前多题列表只问“想查哪一道”，候选出现只说“你看看”，复筛无匹配有时只有“未找到可靠相似题”，结束整页/全部完成等终态也可能没有下一步。这些回复的事实语义应保留，但需要根据业务层真实 `allowed_actions` 补齐选择格式或下一步，所以归入 B 类而不是逐字冻结。

这些事实必须由状态机、章节目录和工具结构化字段提供。输出层只能验证和渲染，不能推断或补写。

### 4.3 C 类：不能冻结，必须整改

以下内容只是当前实现，不是合格基线：

- 任意未知错误经过简单正则清洗后返回；
- `ToolResult.error` 或 `data.rerank_note` 原样进入回复；
- `HTTPException.detail`、`AgentProtocolError` 字符串原样进入 JSON/流式事件；
- 浏览器用 `rawDetail` 或流式 `event.message` 作为未知 code 的用户文案；
- A3 直接修改子 A2 的最终字符串；
- A1 模型生成的预检说明只做长度和少量禁词检查；
- 只断言“包含重试/未找到”等片段，却没有校验协议动作与文案动作一致的测试。

## 5. 风险清单

### P0：必须在第一版输出核心解决

#### OL-P0-01：未知异常仍可能进入 A2 用户回复

- `render.py:173-187` 会调用 `_safe_failure_detail()`；
- `render.py:211-225` 对已知少数错误做映射，但未知错误会清洗后截断返回；
- 因此异常类名、供应商文字、内部字段或未覆盖的路径格式仍可能漏出；
- `test_tiku_agent_agent.py:850-858` 只验证了一个 Windows 路径/超时样本，不能证明未知异常安全。

结论：正则脱敏只能作为纵深防御，不能作为用户文案来源。未知异常必须按稳定 code/category 映射到固定兜底，原文只进日志。

#### OL-P0-02：工具来源字符串存在多条直通路径

- `agent.py:1029-1044` 的 `NEEDS_INPUT` 直接使用 `result.error`；
- `agent.py:1047-1058` 的 PARTIAL notice 直接使用 `result.error` 或 `rerank_note`；
- `agent.py:784-791` 的复筛无匹配直接使用 `reranked.error`；
- `agent.py:880-886` 的答案缺失直接使用 `answered.error`。

当前内置工具大多主动返回友好中文，但契约允许任何调用方构造 `ToolResult(error=...)`，测试替身也证明该字段被视为公开文本。这是“靠每个工具自觉”，不是输出边界。

#### OL-P0-03：普通 HTTP 和流式协议异常可直接公开异常文字

- `fastapi_demo.py:131-193` 对 HTTP/Runtime 协议异常使用 `str(exc.detail)` 或 `str(exc)`；
- `fastapi_demo.py:1379-1405` 在队列/额度流式错误中使用 `str(exc)`；
- `fastapi_demo.py:448-449` 还会把反馈存储的 `ValueError` 原文放进 HTTP detail。

当前构造器传入的队列和额度文字是固定的，但边界本身没有限制来源，未来新增异常很容易泄露细节。

#### OL-P0-04：浏览器对未知服务端错误继续信任原始文字

- `demo_web/demo.js:1214-1231` 对未登记 code 回退 `rawDetail`；
- `demo_web/demo.js:1253-1265` 对流式协议错误直接展示 `event.message`；
- 普通和流式因此可能产生不同文案，也可能绕过服务端安全兜底。

结论：已带协议的服务端错误必须由服务端返回最终安全文案；浏览器不能把未知 `detail` 当公开文本。

#### OL-P0-05：公共 session payload 暴露内部诊断字段

- `a3_runtime.py:2045-2109` 的公共 A3 snapshot 包含 `last_intent`；
- 其中可能有模型生成的 `reason`、`source` 和 `confidence`；
- 单元数据还公开内部自动裁剪 `reason_codes`；
- 浏览器虽然当前没有把所有字段画在页面上，但客户端、历史缓存和浏览器开发工具已经能读取，因此仍属于公开边界。

结论：输出安全不只检查 `text`。公共 payload 必须执行数据最小化，模型 reason、内部 reason code 和 fallback reason 只留服务端日志。

### P1：接入 A2/A3 时解决

#### OL-P1-01：进度消息没有注册表或事实校验

服务端目前有约 15 个 progress 调用点。大多是固定中文，但章节名等动态值可直接插入，Runtime 和流式层会原样转发。进度也属于用户输出，必须按 `progress_key + facts` 渲染。

#### OL-P1-02：协议动作和文案动作没有统一约束

`RequestProtocol` 只有一个主恢复动作，业务文案可能同时提示多个动作。当前没有机器校验以下关系：

- 文案提到“重试”，但协议是否允许重试；
- 文案提到“换章节/全局搜索”，当前状态是否真的开放；
- 文案让用户“选候选”，候选列表和 generation 是否仍有效；
- 文案要求“重新上传”，协议却给出 `retry_request`。

当前已有可达矛盾：不允许消费的 PARTIAL 会把业务 state 设为 ERROR，但仍返回 PARTIAL protocol；`_fail()` 在工具未给动作时会默认 `RETRY_SEARCH`，即使该结果声明不可重试；缺失图片也可能进入声称“题图已保留”的通用错误文案。

#### OL-P1-03：A3 通过字符串后置改写子 A2 回复

`a3_runtime.py:1923-1975` 会根据 A3 题号和剩余数量替换或拼接子 A2 文本。当前测试覆盖了若干主路径，但字符串改写容易：

- 丢失 A2 的部分降级提示；
- 在没有分隔符时粘连句子；
- 用 A3 最新状态覆盖子回复当时的事实；
- 保留子协议但替换成语义不同的文字。

后续应组合结构化消息事实，而不是修改已经渲染完成的字符串。

补充边界：Web 最终 session 会重新读取 A3 snapshot，不会直接序列化子 `AgentResponse.state`；但被改写后的 `text` 仍和子 A2 的 `intent/protocol` 一起公开，A2 的提示和恢复动作仍会丢失，风险结论不变。

#### OL-P1-04：A1 模型回复的终检不足以承担统一安全边界

`image_triage_authority.py:122-159` 已限制长度、公式标记、等待承诺，并对 A3 要求裁剪动作；但生产全流程仍可能在 A1 使用模型说明。A1 当前没有强制要求可执行下一步，也没有系统性阻断内部 route/schema/debug 词。

这不是新增模型需求。后续应保留现有固定兜底，并把模型文本视为唯一的“受约束外部文案”特例；验证不通过立即回退固定文案。

#### OL-P1-05：媒体交付失败时仍可能宣称完整成功

`fastapi_demo.py:1226-1311` 在构造公开 payload 时会忽略无法持久化的候选/答案图片，但业务文本和协议可能仍是 SUCCESS。结果可能出现“答案已经发给你”，实际 `images=[]`。

最终文案必须在媒体持久化后定稿：部分媒体失败为 PARTIAL，全部失败不能保持“已发送”的成功表达。

#### OL-P1-06：A3 动态题目标签缺少公开文本边界

`display_label` 会被直接插入多处 A3 回复；上游解析目前主要做类型检查和 `strip()`，没有公开文本长度/字符约束。应优先使用稳定 page index，标签不合格时退回“图片第 N 题”。

#### OL-P1-07：服务故障可能被误表达为需要人工裁剪

A3 自动定位/校验异常目前会统一降级为 `manual_required`，随后告诉用户需要人工裁剪。服务不可用和图片本身需要人工裁剪不是同一事实，允许动作也可能不同，必须由结构化 reason/category 区分。

#### OL-P1-08：核心响应对象携带完整内部 state

`AgentResponse.state` 在 A2 核心通常由 `state.to_dict()` 填充，其中包含图片路径、候选详情、答案路径、last_error 和 last_intent。当前 FastAPI Web 会重新生成较小的 session snapshot，没有直接序列化这份 state；但其他适配器或未来入口若直接序列化 `AgentResponse` 就会形成泄露。公共输出类型应从结构上排除完整内部 state，不能只依赖当前 Web 恰好没用它。

### P2：维护性问题

#### OL-P2-01：同一错误策略同时存在于 Python 与 JavaScript

上传、网络、服务失败等文案在 `fastapi_demo.py`、`request_protocol.py` 和 `demo.js` 多处重复。新增 code 时很容易只改一边。

#### OL-P2-02：code 粒度与用户语义粒度不一致

A3 多个不同澄清场景都使用 `CLARIFICATION_REQUIRED`；工具 code 又比用户文案细得多。不能直接把 `code` 当模板 ID，需要独立的稳定 `message_key`。

#### OL-P2-03：请求关联与会话失效提示不完整

progress 当前没有 request/search ID；部分没有协议的最终 payload 会在出口新建 request ID。浏览器会话过期还可能只清空历史并显示“准备就绪”，没有明确告诉用户会话已失效和下一步。统一输出契约必须让同一请求的 progress/result/log 共用 ID，并为 session expiry 提供明确动作。

## 6. 现有测试覆盖审计

| 能力 | 已有证据 | 结论 |
| --- | --- | --- |
| 五态协议与旧字段兼容 | `tests/test_request_protocol.py`、`tests/test_tiku_agent_tool_result.py` | 已覆盖机器协议，不覆盖最终文案安全 |
| A2 成功、章节、候选、答案、重试 | `tests/test_tiku_agent_agent.py` | 主流程较完整，很多只断言关键片段 |
| A2 工具 ERROR/NEEDS_INPUT/PARTIAL/NO_MATCH | `tests/test_tiku_agent_agent.py:592-759` | 覆盖行为，但当前测试把工具 `error` 直通当成正确行为 |
| safe answer / reply shell | `tests/test_tiku_agent_safe_answer_*`、`test_tiku_agent_reply_shell_v2.py` | 自身契约严格，但只负责零工具对话 |
| A3 选题、裁剪、取消、完成、子 A2 | `tests/test_a3_runtime.py`、`test_tiku_agent_8790_a3_v1.py` | 主路径和若干精确文案已覆盖 |
| A3 模型异常诊断隔离 | `tests/test_a3_runtime.py:1457-1555` | 已验证内部诊断持久化；缺少用户文本“不含诊断”的显式断言 |
| 普通 API 协议 | `tests/test_tiku_agent_fastapi_demo.py:664-697` | 覆盖 error payload/action；不覆盖恶意 detail |
| 流式 progress/result | `tests/test_tiku_agent_fastapi_demo.py:699-720` | 覆盖顺序和真实 progress；不覆盖 progress 安全 |
| 队列/额度普通与流式错误 | `tests/test_tiku_agent_fastapi_demo.py:755-787` | 固定样本安全；不覆盖任意异常字符串 |
| 上传异常 | `tests/test_tiku_agent_fastapi_demo.py:601-616` | 主要断言 HTTP 状态，不覆盖最终用户文案一致性 |
| 浏览器映射 | `tests/test_tiku_agent_fastapi_demo.py:411-510` 的静态脚本断言 | 证明代码存在，不证明 Python/JS 文案同源或一致 |

### 6.1 后续必须新增的契约测试

阶段 3–5 至少需要补齐：

1. 向 ERROR、NEEDS_INPUT、PARTIAL、NO_MATCH 分别注入包含异常类名、本地路径、URL、token 样式和 schema 原文的字符串，用户文本均不得包含；
2. 普通 JSON 与流式 error 对同一 `message_key` 返回完全相同的 `text/status/code/action`；
3. progress 只接受登记的 key 和白名单事实；
4. 每个模板声明自己提到的动作，并验证它们是业务层 `allowed_actions` 的子集；
5. 动态数字、章节、题号来自 facts，缺失或非法时不得猜测；
6. 未注册 `message_key` 必须记录内部告警，并走与 status/action 一致的安全兜底；
7. A3 组合子 A2 结果后，协议、候选数、题号和剩余题数仍一致；
8. 浏览器对未知服务端 code 不展示 raw detail/event message；
9. A1 受约束模型文本触发禁词、缺动作或超长时回退固定文案。
10. 公共 A3 snapshot 不包含 intent reason/source/confidence 和内部 reason code；
11. 媒体实际交付数与 SUCCESS/PARTIAL/ERROR 及“已发送”文案一致；
12. 当前请求 status 与保留的工作流 phase 分轴表达，寒暄不能因历史 phase=ERROR 而变成 ERROR 响应；
13. 同一请求的普通/流式事件、任务日志使用相同 request/search ID。

## 7. 阶段 1 冻结决定

阶段 1 不修改生产代码。当前冻结的是职责和语义边界：

- A 类固定话术列为兼容基线；
- B 类动态回复冻结业务事实和动作，不冻结现有字符串拼接实现；
- C 类明确不是基线，阶段 3–5 可以改变其用户文字；
- 状态机、检索、章节、候选排序、媒体和计费行为全部保持不变；
- 内部诊断继续保留在日志/状态存储中，但不得成为用户文案输入；
- 现有 A1 结果说明模型调用暂时保留，第一版输出层不新增任何模型调用。

这份分类是下一阶段实现时判断“兼容”与“应修复”的权威依据。逐字文案仍需产品审查；若审查认为某条 A 类话术需要调整，应先在文案目录中显式变更，再更新基线测试，不能由模型或异常分支临时改写。

## 8. 本轮验证记录

2026-08-22 使用项目本地 Python 运行以下相关模块：

```text
tests.test_request_protocol
tests.test_tiku_agent_tool_result
tests.test_tiku_agent_reply_shell_v2
tests.test_image_triage_authority
tests.test_a3_intent_v1
tests.test_a3_web_ui_copy
tests.test_tiku_agent_agent
tests.test_tiku_agent_fastapi_demo
tests.test_a3_runtime
```

结果：`160 tests passed`。这些测试证明本轮文档工作没有伴随生产逻辑回归；它们不能代替阶段 3 所列的新增输出安全矩阵。

另外已验证：两份阶段文档引用的所有本地 `path:line` 均存在且行号未越界，方向计划中的相对链接可解析。
