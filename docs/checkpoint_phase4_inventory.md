# 阶段 4 中间结果现状盘点

## 目的

阶段 4 不再建立第二套日志。现有 Trace 继续作为问题排查入口；Checkpoint 保存 Trace
无法承载的关键业务结果，Artifact 保存需要按权限查看的短期图片证据。固定下钻关系为：

```text
trace_id -> checkpoint_id -> artifact_id
```

这里的 Checkpoint 是不可变诊断证据，不是可执行任务状态。它不能授权前端动作、覆盖
`TaskStateSnapshotV1`、自动重放模型/工具调用，或提前实现阶段 5 的幂等控制、阶段 6 的后台
任务恢复。

Checkpoint/Artifact 也不是永久历史库。4.1 当前只有纯数据契约，没有任何生产写入；4.2 先实现
并验收生命周期控制面，4.3/4.4 接入后才让有效搜索 best-effort 进入有限滚动窗口。图片按物理
Artifact 只保存一份，允许多个兼容 Checkpoint 引用同一个 `artifact_id`。

## “每次搜索”边界

4.3/4.4 的目标采集范围覆盖每个已经通过认证、配额、队列和上传准入并真正进入业务处理的逻辑
搜索：

- A3 父任务按 `workflow_search_id + workflow_task_revision` 关联；
- A3 子题同时保存父 `workflow_task_revision` 和子 A2 `task_revision`，并按
  `workflow_search_id + search_id + unit_id` 关联；
- standalone A2 显式令 `workflow_search_id == search_id`；
- 同题重试保留 `search_id`，但新网络操作使用新的 `trace_id`；
- health、登录、配额拒绝、队列拒绝、媒体读取等仅有 Trace，不创建业务 Checkpoint；
- 不记录进度消息、函数调用或每次状态保存，只记录权威业务结果发生变化的阶段边界。

因此，“每次搜索都保存”是目标覆盖范围，不是永久保存承诺，也不是“每个 HTTP Trace 都复制
一份完整状态”。一次搜索可以跨多个 Trace；每个成功写入的 Checkpoint 保存产生它的 Trace，
同时由业务 ID、revision 和前驱 Checkpoint 串成证据链。写入是 best-effort：只保留 TTL 和容量
窗口内的记录；达限后停止新增诊断证据、搜索仍继续并把 health 标为 degraded，不会无限追加。

## 当前数据来源

| 数据 | 当前来源 | 可复用内容 | 不能直接迁入的内容 |
| --- | --- | --- | --- |
| HTTP/模型/工具时间线 | `tiku_shared/trace_events.py` | `trace_id`、阶段、结果码、次数、耗时、模型名 | 原图、识别文字、候选详情、路径、异常正文 |
| 最终公共结果 | `tiku_shared/response_store.py` | `response_id`、父子业务 ID、phase、候选数、协议结果 | 用户问题、回复全文、图片和内部路径 |
| A2 活状态 | `tiku_agent/state.py` | 章节、荷载、结构、候选、revision/generation | 整个对象含图片、候选和答案路径，不可直接序列化 |
| A3 活状态 | `tiku_agent/a3_runtime.py` | 整页理解、unit、bbox、裁图校验、父子 ID | 整个对象含原图/裁图/overlay 路径和任意错误正文 |
| 粗筛/复筛结果 | `tiku_agent/tools.py` | 分层数量、候选分数、查询题尺寸识别/过滤、复筛状态 | 题库绝对路径、完整候选和未限制错误文本 |
| 会话图片 | `tiku_agent/session_artifacts.py` | session 根隔离、安全文件名解析 | 没有 artifact ID、哈希、大小、类型、独立 TTL 和审计 |
| 用户反馈证据 | `tiku_agent/feedback_store.py` | 有界对话/媒体复制、反馈归属和过期 | 只在用户评分时产生，不能冒充每次搜索证据库 |
| 清理入口 | `tiku_diagnostics/retention.py` | plan/confirm/apply、路径防护、漂移检查 | Trace 无批准策略，尚无周期调度和真实 apply 基线 |

## 已验证缺口

1. Trace 是安全的事件摘要，但不能解释模型具体识别了哪些题、荷载、结构和尺寸，也不能展示
   每个候选的粗筛/复筛分数。
2. A2/A3 session store 保存的是会变化的完整状态，默认约两小时；它不是不可变历史，且含有
   绝对路径和不适合长期保留的内容。
3. A3 page-error、stderr 和部分旧 task log 的字段与失败覆盖不一致；裁图写入、overlay、裁图
   校验和复筛回退仍可能只能从零散状态推断。
4. 当前媒体文件只有 session 目录和随机文件名，不能通过一个受控 ID 完成归属、到期、查看、
   延长或删除审计。
5. 只靠 TTL 不足以证明存储有界；Checkpoint、Artifact、证据审计、Trace 行数，Artifact 总字节数、
   单 Checkpoint Artifact 数量和磁盘最小剩余空间都必须有硬限制。
6. 现有 Trace 尚未纳入批准的周期清理策略。即使新 Artifact 有界，Trace 不清理仍会持续增长；
   因此 `max_trace_rows` 和批准范围内的周期清理同属 4.2 准入门。
7. 现有上传入口接受 JPEG/PNG/WEBP/GIF/BMP，契约若只登记前三种会把合法输入错误拒绝。
8. 当前父 A3 与子 A2 都使用名为 `task_revision` 的字段，但生命周期不同；Checkpoint 若只留
   一个 revision，会把父任务版本和子候选 generation 错误混为一体。
9. 当前没有统一、确定性的阶段输入指纹，不能安全判断跨 Trace 的同一阶段结果是否可有限复用。

## 关键结果与当前映射

下表是后续运行时采集目标，不是 4.1 已经落盘的现状。4.2 只建立 Store/TTL/容量/清理；4.3 必须
从 A2 runtime 及实际工具返回中组装完整的单题摘要，4.4 再接 A3 整页和裁图摘要。

| Checkpoint | 必须回答的问题 | 当前主要来源 | 4.3/4.4 需要补的采集能力 |
| --- | --- | --- | --- |
| `image_accepted` | 上传图的哈希、JPEG/PNG/WEBP/GIF/BMP 实际格式、字节、像素和方向处理是什么 | 上传校验、Pillow、session artifact | 生成 `artifact_id`，禁止记录原文件名/路径 |
| `image_routed` | 为什么进入 A1/A2/A3 | 图像权威分流、Trace route event | 保存稳定 reason code/版本，不保存模型原文 |
| `page_understood` | 识别了几组、几题、哪些 unit、可否检索 | `A3PageUnderstanding`、A3 session | 保存总数/保存数/截断标志和最多 50 个结构化 unit；不能假定最多 10 题 |
| `crop_prepared` | 哪个 unit、什么 bbox、如何裁剪、对应哪张图 | `auto_crops`、GLM bbox、Pillow 输出 | 原图/裁图/overlay 改为 artifact 关联 |
| `crop_validated` | 六项门禁结果是什么、为何转人工 | `CropCompareResult`、external-load screen | 稳定 reason code，禁止任意异常正文 |
| `question_analyzed` | 章节、结构和荷载具体是什么 | A2/A3 unit analysis、structure tool | 荷载保留标准类型及有界 `raw`；不提前声称已获得粗筛内部才识别的工程尺寸 |
| `coarse_search_completed` | 查询题识别出什么工程尺寸、各粗筛层有多少候选、哪些过滤生效 | `coarse_search_tool`、dimension filter | 每个尺寸结果保存 status/reason code，空结果用 missing/not-run sentinel；保存截至 `after_dimension_filter` 的计数和有限候选 ID，不伪造候选 `visible` |
| `rerank_completed` | 输入/成功/失败/最终可见各多少、阈值及每题分数 | `rerank_candidates_tool`、normalized candidates | 在 rerank policy 保存 `visible` 及本阶段截断字段；每条候选有 `visible` 布尔值，所有展示候选必须进入明细 |
| `answer_prepared` | 用户选择了哪个候选、生成了哪些答案媒体，或为何没有匹配答案 | Agent state、media post-processing、Response Store | `no_match` 固定为 `answer_artifact_count=0`、`media_status=not_available`、`delivery_code=NO_MATCH`、无 `answer_image` Artifact/selection；最终交付仍以 Response 为权威 |

### 关键 outcome 边界

- 裁图 `external_load_status` 只允许 `not_run/not_configured/yes/no/error`。六项检查全真时才是
  `verified`：搭配 `yes/not_configured` 可为 `success/partial`，搭配 `no/error` 必须为
  `needs_input`；`review_required` 只能搭配 `not_run + needs_input`。归一化 bbox 必须满足
  `0 <= x1 < x2 <= 1000`、`0 <= y1 < y2 <= 1000`；像素 bounds 必须恰有四个非负整数、在源图内，
  且裁图宽高精确等于边界差。
- 粗筛允许 `success/partial/no_match/failed`。非失败计数必须满足
  `chapter_scanned >= load_scored >= positive_score >= rerank_pool >= after_dimension_filter`；
  `success/partial` 要求过滤后和保存明细数都大于 0，`no_match` 要求过滤后为 0，从而保存明细
  也为 0 且未截断。粗筛候选禁止提前写 `visible`。
- 复筛允许 `success/partial/no_match/skipped/failed`。`success` 必须完整复筛、零失败、无 fallback
  且至少一项可见；`no_match` 必须零可见且无 fallback；`partial/skipped` 必须未复筛完成、使用
  fallback 且至少一项可见，其中 `skipped` 的完成/失败数都为 0。每条复筛候选必须有 boolean
  `visible`，真值数精确等于 policy 的 `visible`，所有可见项都必须保存在最多 50 条明细中。
- `failed` 可以只保存严格 failure，不要求伪造尚未形成的正常结果段。全部精确计数、截断和
  outcome 不变量以 `checkpoint_v1_contract.md` 的冻结矩阵为准。

## 数据分层

### 4.3/4.4 接入后 best-effort 保存的结构化内容

- 必填 `identity_key`，以及 session、父子业务 ID、父 `workflow_task_revision`、scope-local
  `task_revision`、stage、outcome、发生时间和生产版本；
- 由 `compute_input_fingerprint_v1` 计算的 `input_fingerprint` digest：将
  `contract=intermediate_checkpoint`、`schema_version=1`、stage、完整 owner、完整 producer 和
  1～50 个按名称排序的输入 SHA-256 digest 组成 canonical JSON；用 `ensure_ascii=true`、递归
  key 排序、无空白分隔符编码 UTF-8 后再取 SHA-256。原始输入、路径、时间和 Trace/Request/
  Checkpoint ID 不进入计算；
- 图片哈希、格式、字节和像素尺寸；
- 题目数量、unit、bbox、裁图/校验状态；
- 章节、结构类型、规范化荷载和有界 `raw`；
- 粗筛阶段识别出的工程尺寸及其来源、状态、reason code；未运行或未识别必须使用有界 sentinel，
  不能用空列表表达两种不同含义；
- 各过滤层候选数量、有限候选 ID、粗筛/复筛/综合分和阈值；最终 `visible` 只属于复筛/回退
  决策，复筛明细逐项标记且必须覆盖全部展示候选；
- 稳定失败码、错误类别、是否可重试、回退方式和最后成功检查点。

### 只作为短期 Artifact 保存

- 上传原图；
- A3 单题裁图；
- A3 overlay；
- 诊断确有需要的有限候选图；
- 已准备交付的答案图。

图片二进制不得放进 SQLite、Trace 或公共 JSON。Checkpoint 只保存受约束
`artifact_id + role + ordinal`。

同一上传原图、裁图或 overlay 不得按 Checkpoint 重复复制；第一次登记后，后续阶段复用同一
`artifact_id`。引用不续期，Artifact 到期后所有引用均不可读。

### 默认禁止

- 完整 Prompt、完整模型原文、reasoning、全文 OCR；
- 完整用户对话、完整题库记录和无限候选列表；
- API key、token、邀请码、Cookie、管理员凭据；
- 本地绝对路径、URL、原文件名、storage key；
- traceback、任意异常正文或整个 `AgentState` / `A3SessionState` / `ToolResult`；
- 在公共响应中返回 `trace_id`、`checkpoint_id`、`artifact_id`、内容哈希或身份键。

Producer 的组件、模型和版本类值还必须是 1～128 字的安全 symbol：以字母或数字开头，后续只
允许字母、数字、`_ . + -`；模型 provider/name 必须成对。所有文本统一拒绝绝对路径、以盘符
开头的 drive-relative 路径（如 `C:private`）、URL、Bearer/`sk-...` 以及敏感键的
`key=value`/`key:value` 形态。这里的敏感键是 token、cookie、password、secret 等明确集合，
不是任意等号表达式；普通力学符号 `P`、`L` 和识别摘录 `P/L=2` 仍然合法。

## 所有权与访问

- Checkpoint 和 Artifact 都绑定 `session_key`、必填且非空的 `identity_key`、
  `workflow_search_id`、可选的 `search_id/unit_id`、父 `workflow_task_revision` 及 scope-local
  `task_revision`。
- `artifact_id` 只是不可猜测定位符，不是访问令牌；读取时必须重新验证 owner、状态和到期时间。
- Artifact 到期后先变为不可访问 tombstone，再清理物理文件；物理删除失败不能恢复访问。
- `source_page` 可以由同 workflow/父 revision 的 workflow 与 child 引用；`question_crop` 和
  `crop_overlay` 只允许同 workflow/unit 的 crop 阶段及其 child 引用；候选图和答案图要求完整
  child owner 兼容。跨 session、identity 或 workflow 一律禁止。
- 内容哈希可用于底层物理去重，但不同 owner 必须保留独立逻辑记录和访问控制；实现前不得默认
  开启跨 owner descriptor 复用。
- 查看 Checkpoint、查看 Artifact、延长保留、人工删除和自动清理都必须写无敏感内容的审计；
  查看审计无法提交时不得返回证据。

上述跨 scope role 矩阵是 4.1 冻结的授权规则，不是当前两个独立纯对象构造器已经完成的联合
校验。4.2 Store 解析 `artifact_id` 后必须取出 descriptor、执行矩阵并覆盖允许/拒绝测试，之后
才能返回 Artifact。

## 保留和容量结论

初始策略冻结为：普通结构化 Checkpoint 30 天；普通图片 3 天；明确失败图片 7 天；反馈图片
默认 30 天并沿用既有可配置上限 365 天；调查证据默认 30 天、上限 90 天。延长必须显式、
有原因并有新期限，Trace 元数据不能隐式延长内容。

这是按时间滚动而不是按用户永久累加：每天新搜索进入窗口、到期搜索退出窗口。普通
`success/no_match/skipped` 结构均为 30 天，图片通常更早在第 3 天退出；只有明确失败图片默认
保留 7 天。长期统计只能是去除题图、识别文字和个人业务标识的聚合结果。

部署必须显式配置七项正数：`max_checkpoint_rows`、`max_artifact_rows`、`max_audit_rows`、
`max_trace_rows`、`max_artifact_bytes`、`min_free_bytes` 和 `max_artifacts_per_checkpoint`；最后一项
还限制为 1～50，任何一项都不得依赖隐式默认值。4.1 不在缺少生产搜索量、图片分布和磁盘余量
证据时猜具体容量。
其中 `max_audit_rows` 只指 Checkpoint/Artifact 证据链的查看与维护审计，不包含 8795 控制库
管理员审计。4.2 还必须冻结证据审计的有限保留/轮转规则，并为 `auto_purge` 预留写入空间，防止
审计容量满后阻塞清理；现有控制库管理员审计策略不在本阶段改动范围内。
后续实现应先清过期项；仍超限时停止新增诊断证据、健康状态降级，但搜索业务继续，且不得因此
重放模型或工具调用。这意味着目标是覆盖每次已准入搜索，而不是以牺牲业务可用性换取“每次必存”。

4.1 构造器目前只校验带时区的创建/到期时间顺序与最大间隔，不读取系统当前时间。4.2 必须由
真实 Store 使用可信服务端当前时间完成 TTL 生成和读取拒绝，并完成写前容量闸门、周期 retention
plan/apply、孤儿清理、清理审计与释放结果验证。现有 retention 默认 dry-run，且尚无周期调度
和真实 apply，因此当前不满足准入条件。在这些能力闭环前，禁止接入 A2/A3 全量
Checkpoint/Artifact 采集；只能使用受控样本验证 Store 和清理链。

## 写入失败边界

- Checkpoint/Artifact 保存、队列满或容量不足：业务 fail-open，增加安全健康计数；
- Response Store 最终响应绑定：继续保持现有 fail-closed；
- Checkpoint/Artifact 的 ID、schema/decode、owner、状态、期限或路径解析任一失败：读取
  fail-closed；
- Checkpoint 用于有限复用时，owner、`input_fingerprint` 或生产版本不一致：禁止复用并重新
  计算；
- 查看、延期、人工删除的审计无法落盘：对应读取或管理动作 fail-closed；
- retention 的计划、哈希、备份、路径或执行对象漂移：维护操作 fail-closed。

## 4.1 后仍未实现

- SQLite Checkpoint/Artifact Store 和写入队列；
- 图片复制、受控解析、物理去重和引用计数；
- Store 解引用时的 Artifact 跨 scope owner-role 联合授权校验及允许/拒绝测试；
- TTL 执行、容量闸门、周期 apply、孤儿修复和清理空间验证；这些是 4.2 接入 A2/A3 前置门；
- 4.3 A2 各阶段的完整 runtime/tool 摘要采集；前置门未通过时不得开始全量采集；
- 4.4 A3 整页、unit、裁图校验和父子关联采集；
- Trace 中提交后 `checkpoint_id` 关联；
- 4.5 诊断下钻、管理员查看和审计；
- 任何执行幂等、HTTP 解耦或暂停/继续行为。
