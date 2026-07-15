# Decision Trace Lab：主线观测分线规格

## 0. 严重目标纠偏与规格优先级

本规格取代 `10_product_web_spec.md`，并取代 `01_product_spec.md`、`04_product_revision_round1.md` 中与“另写私有 Agent、用替代编排验证轨迹”有关的冲突内容。

仍然有效的旧要求只有：独立目录/端口/runtime、只读题库、隐私受控、fail-open 轨迹、人工三态标签、NO_MATCH 分类、汇总与 scan、不影响 8790/8788 和主线工作树。

目标从现在起只有一个：

> 建立当前 main 的真实 Agent 与真实网页 Demo 的行为镜像，只在旁路增加决策轨迹和人工标注；不得重新实现、简化、近似或改造业务流程。

当前 8793 使用新写的 `private_agent.py` 和 `web_app.py`，其中重新定义了 `ActionDecisionV2`、`AgentState`、`authorize_action_v2`、`decide_intent_v2`、`TikuSearchAgent`、`SessionStore`、`RealProjectBackend` 和 `WebOfflineBackend`。它与主线 `tiku_agent/agent.py`、`intent_v2.py`、`state.py`、`tools.py`、`session_runtime.py`、`fastapi_demo.py` 不是同一实现，因此不能作为主线效果观测入口。

## 1. 产品定义

新 8793 是“主线观测分线”，不是“另一个搜题 Agent”。

用户在 8793 左侧看到和主线 8790 一致的搜题页面、文案、上传、进度、追问、候选、答案、失败恢复和会话行为；右侧额外看到同一真实回合的：

`Intent → 授权 → 工具调用 → AgentState 转换 → AgentResponse`

并可对关键事件标 `correct / incorrect / uncertain`，对 NO_MATCH 标 `reasonable / false / uncertain`。

除了新增观测副作用，8793 的业务结果必须等价于同一版本的主线。

## 2. 镜像来源与快照完整性

### 2.1 来源

- 源仓库：`F:\cc\7-题库检索`
- 来源分支：`main`
- 本次核对 HEAD：`bc27cba1339f8a73aee18c4a44e109cecd84bd3d`
- 快照前要求：主仓 `git status --short` 为空；若不为空，先停止，不能把未知未提交改动当 main 快照。

执行时应再次读取 HEAD；如果已变化，则 manifest 记录新的实际 main commit，测试基准也必须使用同一个 commit，不能继续声称基于上述旧 SHA。

### 2.2 快照方式

在 Lab 内建立新的只读代码镜像，例如：

```text
decision_trace_lab/
├─ mainline_mirror/
│  ├─ source/            # 当前 main 的代码镜像
│  ├─ manifest.json      # commit、相对路径、SHA-256
│  └─ observation/       # 旁路观测适配器；不得混进 source
├─ runtime/mainline_web/
├─ data/
└─ tests/mainline_parity/
```

不得在已有 `source_snapshot/tiku_agent/tools.py` 单文件快照上继续扩建；它不足以代表完整主线 Agent。

推荐复制当前 main 的完整受版本控制代码树，再排除 `.git`、本地 config、密钥、生产 runtime、题库资产、日志和缓存。至少必须包含并逐文件哈希：

- `tiku_agent/agent.py`
- `tiku_agent/intent_v2.py`
- `tiku_agent/action_decision_v2.py`
- `tiku_agent/action_permissions_v2.py`
- `tiku_agent/conversation_context_v2.py`
- `tiku_agent/intent_runtime_v2.py`
- `tiku_agent/intent_contract.py`
- `tiku_agent/reply_shell_v2.py`
- `tiku_agent/render.py`
- `tiku_agent/state.py`
- `tiku_agent/tools.py`
- `tiku_agent/session_runtime.py`
- `tiku_agent/session_store.py`
- `tiku_agent/session_artifacts.py`
- `tiku_agent/task_log.py`
- `tiku_agent/fastapi_demo.py`
- `tiku_agent/demo_web/*`
- 上述模块真实 import 到的主仓代码，如 `search.py`、`multi_agent_pipeline.py` 和相关 `scripts/*`

manifest 至少记录：

```json
{
  "source_repository": "F:/cc/7-题库检索",
  "source_branch": "main",
  "source_commit": "<实际 SHA>",
  "created_at": "<ISO 8601>",
  "files": [{"path":"tiku_agent/agent.py","sha256":"..."}]
}
```

启动 8793 前必须校验所有镜像文件哈希；任何缺失或不匹配都拒绝启动，不能静默退回 `private_agent.py`。

### 2.3 依赖与数据

- 业务代码从 `mainline_mirror/source` 导入，不从正在运行的 8790 进程导入。
- 测试中的主线基准与观测分线必须加载同一 commit 的同一业务代码。
- 真实题库可作为只读数据源显式配置；不得复制或读取生产密钥配置。
- 依赖注入只允许替代外部模型、文件系统数据和时间/UUID等非确定性边界，不允许替代 Intent、授权、Agent、状态机或工具编排。

## 3. 业务行为不可变原则

### 3.1 必须直接使用的主线组件

观测分线必须直接执行镜像中的：

- `tiku_agent.intent_v2.decide_intent_v2`
- `tiku_agent.action_permissions_v2.authorize_action_v2`
- `tiku_agent.agent.TikuSearchAgent`
- `tiku_agent.agent.AgentToolbox`
- `tiku_agent.state.AgentState`
- `tiku_agent.session_runtime.AgentSessionRuntime`
- `tiku_agent.fastapi_demo` 的上传、会话、流式响应和媒体流程
- `tiku_agent.demo_web` 的左侧页面交互

不得复制函数内容后改名重写，不得创建“兼容版本”或“简化版”。

### 3.2 透明观测定义

观测适配器只能做：

1. 在调用前读取脱敏摘要；
2. 调用原主线对象/函数恰好一次；
3. 在调用后读取脱敏摘要；
4. 原样返回同一个结果对象或值；
5. 轨迹写入失败时吞掉观测异常并记录独立诊断，不改变主线异常、返回值、状态或调用次序。

禁止观测器：

- 修改参数、prompt、上下文或 ToolResult；
- 捕获主线异常后替换成另一种业务结果；
- 增加业务重试、跳过工具、改变超时、改变并发数；
- 根据日志重新推断并替代真实授权结果；
- 修改 AgentState 后再“还原”；
- 为了方便观测而拆分或合并主线工具；
- 调用原函数两次；
- 让轨迹是否成功决定 Agent 是否继续。

## 4. 观测架构

### 4.1 回合外壳

用透明 `ObservedAgent` 包装主线 `TikuSearchAgent`：

- `handle_image`/`handle_text` 前创建 turn；
- 内部仍调用主线 agent 的原方法一次；
- 返回主线 `AgentResponse` 原对象/等价无修改结果；
- 回合结束后写 `turn_completed`；
- 观测写入失败不得影响返回。

通过主线 `AgentSessionRuntime.agent_factory` 注入这个包装器，仍由主线 runtime 负责恢复/保存主线 AgentState、进度、媒体和回合生命周期。

### 4.2 Intent 与授权

不得新写 `decide_intent_v2` 或 `authorize_action_v2`。

允许在镜像进程内对主线模块引用安装透明 hook：hook 调用原函数一次，记录真实入参与真实返回，再原样返回。

必须记录：

- `decide_intent_v2` 最终真实 `ActionDecisionV2`；
- 每次真实 `authorize_action_v2` 的 decision/context 摘要和 AuthorizationResult；
- source、action、编号、chapter override、clarification/reject code；
- 每回合授权调用真实次数。

若主线某条规则路径在内部多次或零次调用授权，轨迹应忠实反映；不得为了满足旧 schema 人造“每回合恰好一次”。新的验收以“与主线实际调用序列一致”为准。

### 4.3 工具调用

用透明 `ObservedToolbox` 包装主线 `AgentToolbox` 的九个 callable。每个 wrapper：

- 保持原函数签名接受方式；
- 记录脱敏输入摘要；
- 调原 callable 一次；
- 记录同一 ToolResult 的 `ok/next_state` 和白名单输出摘要；
- 原样返回 ToolResult；
- 保持原工具调用顺序、次数、异常和耗时行为。

工具轨迹序列必须来自真实调用，不得事后按 phase 推断。

### 4.4 状态转换

状态对象必须是主线 `AgentState`。

可在已观测的主线边界前后对 `AgentState.to_dict()` 做脱敏 diff，或给主线状态方法安装透明 hook；不得使用 Lab 自定义 State。

轨迹需要区分：

- ToolResult 的建议 `next_state`；
- 主线 AgentState 的实际 `phase`；
- 哪个真实方法/工具触发变化。

如果一个业务动作内部有多次状态变化，必须按真实顺序记录，不得只写回合前后状态。

## 5. 网页镜像要求

### 5.1 左侧必须是主线 Demo

8793 左侧以当前 main 的 `fastapi_demo.py` 与 `demo_web/index.html`、`demo.css`、`demo.js` 为基线。

以下必须与同版本主线一致：

- 页面文案；
- 上传、拖放、客户端图片规范化；
- 最大大小与支持格式；
- 文本输入、Enter/Shift+Enter；
- progress 文案和时序；
- 单题/多题追问；
- 章节选择；
- 候选图片、编号、候选按钮发送文本；
- 答案图片和大图；
- 错误、重试、取消、新会话；
- 刷新后的会话/图片恢复；
- Agent 返回 text、images、intent 的处理。

不得由新 `web_app.py` 根据 phase 自行生成“近似文案”或伪造候选/答案。

### 5.2 右侧新增观测栏

唯一产品新增是右侧评测侧栏：

- 当前回合时间线；
- Intent、授权、工具、状态事件；
- correct/incorrect/uncertain 标签；
- NO_MATCH 三分类；
- 汇总和 scan。

侧栏不能影响左侧请求、busy 状态、候选选择和 Agent 响应。侧栏加载或标注失败时，左侧仍与主线一致。

### 5.3 允许的 Web 基础设施差异

以下差异属于隔离，不算业务差异：

- 端口 8793，而非 8790；
- host 仅 `127.0.0.1`；
- 独立 Cookie 名；
- 独立 runtime、session DB、artifacts、task log；
- 无 tunnel、无公网、无 HSTS/forwarded-proto 强制跳转；
- 增加 trace/label/summary/scan API；
- Agent result 响应额外携带不影响左侧解析的 `turn_id/trace_complete`，或通过独立 header/查询接口关联。

候选/答案媒体随机 ID 和绝对耗时/UUID可以不同；业务内容和次序不能不同。

## 6. 允许改动白名单

`mainline_mirror/source` 中业务快照应保持逐字节一致。改动只能位于独立 observation/Web 扩展层，白名单如下：

1. 新增透明观测包装器和 TraceRecorder。
2. 新增标签、汇总、scan 模块/API。
3. 将默认 runtime/session/artifacts/task-log 路径注入为 Lab 路径。
4. 更换 Cookie 名、host、port，移除公网 tunnel/HTTPS 代理假设。
5. 在原页面外层增加右侧栏容器和对应独立 CSS/JS；左侧原 DOM、文案和事件逻辑保持不变。
6. 在主线 JSON/stream result 上增加向后兼容的观测关联字段，不删除、不改名、不改变已有字段。
7. 测试时注入确定性的 LLM/tool 外部依赖返回。

不在白名单中的差异默认禁止。

## 7. 明确退役旧实现

以下对象不再是有效运行或验收入口：

- `app/decision_trace_lab/private_agent.py` 中自定义 Agent/Intent/授权/State/Toolbox；
- `app/decision_trace_lab/web_app.py` 当前基于 private_agent 的业务流程；
- `WebOfflineBackend`；
- `OfflineInjectedBackend` 作为端到端业务入口；
- `run_web_demo_offline.py`；
- 任何依赖 `private_agent.TikuSearchAgent` 的 CLI/Web 启动命令。

它们可以移动到明确的 `archive/invalid_reimplementation/` 留档，但：

- 默认 8793 启动入口不得 import 它们；
- README 不得推荐它们；
- 自动测试不得把它们的成功当主线 parity 证据；
- 尝试 `--offline-injected` 或旧启动脚本应明确拒绝并提示“已退役”，不能静默启动。

测试替身只能注入到主线原组件的外部依赖边界，不能通过旧私有 Agent 运行。

## 8. 差分测试总原则

同一固定输入、同一初始主线 AgentState、同一确定性外部依赖返回下，同时运行：

1. 基准：未安装观测 hook 的 mainline mirror；
2. 观测：安装透明 hook、独立 recorder 的同一 mainline mirror。

必须逐项相等：

- `AgentResponse.text`
- `AgentResponse.images`（在相同 fake media 输入时精确相等；独立 runtime 实测时做内容哈希/逻辑 ID 归一化）
- `AgentResponse.state`
- `AgentResponse.intent`
- 回合结束 `AgentState.to_dict()`
- 主线 Intent 返回对象字段
- 真实授权调用次数、入参摘要和结果序列
- 工具名称、调用次序、次数
- 每个工具接收的业务参数摘要
- ToolResult 的 `ok/data/error/next_state`（路径/UUID只做明确归一化）
- 抛出的异常类型和发生边界

唯一允许新增的是 trace/diagnostic/label 文件和右侧 UI/API 副作用。

## 9. Agent 级差分测试矩阵

至少覆盖以下场景，每个场景都执行 baseline vs observed：

| 场景 | 必比重点 |
|---|---|
| 发单题图，章节未知 | WAIT_CHAPTER、全局兜底选项、工具序列 |
| 发单题图，章节可确定 | route→structure→coarse→rerank、候选结果 |
| 发多题图 | 多题识别、裁图、WAIT_QUESTION_CHOICE |
| 多题选择题号 | question_index、章节处理、后续搜索 |
| 设置/纠正章节 | revision/state 清理、重新搜索 |
| 选择候选 | candidate_rank、答案工具、ANSWERED |
| 候选都不对 | reject_candidates 状态与回复 |
| 继续搜索 | 排除已尝试候选、工具参数与序列 |
| 回看候选/重发答案 | 不必要工具不得新增 |
| 答案不匹配 | 状态标记与安全回复 |
| greeting/small talk/help | 不调用题库工具，不改业务状态 |
| 模糊编号 | question/candidate 命名空间、clarification |
| LLM 兜底意图 | 同结构化模型返回下 action 完全一致 |
| 未授权 global_search | 同拒绝/追问，不调用全局工具 |
| 已授权 global_search | 调用一次、同阈值与同候选 |
| NO_MATCH | 同 phase、同文案、同恢复能力 |
| 工具异常/复筛不完整 | 同错误、同状态、同重试边界 |
| cancel/retry/explain_failure | 同状态、回复和工具调用 |

另做 recorder 故障注入：磁盘写失败、隐私拒绝、serializer 异常时，所有上述业务比较仍须完全相等。

## 10. HTTP 与前端差分测试矩阵

用同一确定性 AgentFactory/外部依赖启动：

- baseline：镜像主线 `fastapi_demo.create_app`；
- observed：8793 主线观测 App。

覆盖：

1. `GET /`：左侧 DOM 文案、ID、输入和候选结构与主线一致；只允许多出右侧栏及关联资源。
2. `GET /api/session`：已有主线字段和值一致；观测端可新增字段。
3. 文本普通/stream：状态码、progress 序列、最终 text/images/intent 一致。
4. 图片普通/stream：格式验证、15MB、multipart、临时清理、progress 和结果一致。
5. 候选按钮：发送的文字与主线 `选择候选 N` 完全一致。
6. reset：业务状态和左侧历史行为一致，观测历史按独立规则保留。
7. upload/media：同会话归属和路径穿越拒绝行为一致。
8. 超时、网络失败、Agent 异常：左侧安全文案一致。
9. 页面刷新：主线会话恢复行为一致；右侧额外恢复脱敏 turn 列表。
10. 侧栏 API 全部失败时，左侧仍通过主线测试。

前端可用 DOM/截图冒烟确认左侧布局未被右侧 CSS 污染；390×520 等主线已关注视口仍需复跑。

## 11. 轨迹与人工标注

轨迹 schema 可以沿用现有事件名称，但内容必须来自真实主线边界：

- `turn_started`
- `intent_decided`
- `authorization_checked`（按真实调用次数）
- `tool_started/tool_completed`
- `state_transition`
- `turn_completed`

人工标签继续使用 `correct / incorrect / uncertain`；未标为 unlabeled。NO_MATCH 继续分 `reasonable_no_match / false_no_match / uncertain_no_match`。

标签、scan、汇总不会参与主线 Agent 决策。原始用户文字、完整图片路径、完整候选、prompt/模型原文、密钥不得落轨迹。

## 12. 隔离与运行

- 新入口建议：`run_mainline_observed_web.py`。
- 只监听 `127.0.0.1:8793`。
- runtime：`decision_trace_lab/runtime/mainline_web`。
- data：`decision_trace_lab/data`。
- 独立 Cookie、SQLite、session artifacts、incoming、media、answer_output 和 task log。
- 不读取生产 8790/8788 session、cookie、runtime、task log。
- 不停止、重启、调用或代理 8790/8788。
- 题库真实运行只读；写工具不进入观测分线。
- 不修改 `F:\cc\7-题库检索` 工作树，不提交 Lab 文件到主仓。

## 13. 验收失败条件

出现任一项即 FAIL：

1. 默认入口仍 import/实例化 `private_agent.TikuSearchAgent`、自定义 Intent/Authorization/AgentState 或 WebOfflineBackend。
2. mainline mirror manifest 缺 commit/hash，或启动时不校验、校验失败仍继续。
3. baseline vs observed 任一场景的 AgentResponse、final state、intent、工具序列/次数不一致。
4. 为记录轨迹改变参数、返回值、异常、工具次序、重试、超时或 phase。
5. recorder/label/scan 失败改变左侧 Agent 结果。
6. 左侧文案、候选选择文本、答案/图片处理或会话行为与同版本主线不同。
7. 轨迹是按最终状态推断或由 fixture 拼出，而非真实 hook 采集。
8. 旧 offline/private Agent 测试被用作 parity 通过证据。
9. 默认不是 real 主线镜像，或可在页面静默切换到旧 fake Agent。
10. 写入/读取生产 runtime、Cookie、端口、tunnel、配置或密钥文件。
11. 写题库、修复索引、删除/入库，或修改主仓工作树。
12. 轨迹/标签/响应泄露用户原文、绝对路径、完整模型输出或凭据。
13. 右侧 CSS/JS 破坏主线左侧交互或移动端布局。
14. 8793 无法证明自己运行的 source commit，或镜像与测试基准不是同一 commit。

## 14. MVP 放行标准

只有同时满足以下条件才能称为“主线观测分线”：

1. 默认 8793 运行哈希校验通过的当前 main 镜像。
2. 左侧通过主线 Demo HTTP/前端差分测试。
3. Agent 级完整差分矩阵全部相等。
4. 轨迹来自真实主线 Intent、授权、工具、State 边界。
5. recorder fail-open 故障矩阵不造成任何业务差异。
6. 右侧能标关键事件、NO_MATCH、查看汇总/scan，且不反向影响业务。
7. 旧 private_agent/WebOfflineBackend 已从有效入口和验收入口退役。
8. 主仓、8790、8788、生产 runtime 和题库写入均未受影响。

一句话验收口径：

> 把观测全部关闭后，8793 左侧必须就是同版本主线；把观测打开后，唯一变化只能是多了可失败但不干扰业务的 trace 和右侧评测栏。
