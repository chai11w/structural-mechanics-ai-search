# Intermediate Checkpoint V1 契约

## 状态

本文件冻结阶段 4.1 的内部契约。对应可执行词汇和验证位于
`tiku_agent/checkpoint_contract.py`，契约测试位于 `tests/test_checkpoint_contract.py`。

V1 只定义纯数据，不进行 I/O，不接入 8790/8896，不改变 A2/A3 业务行为、检索排序、计费、
公共协议或前端动作授权。因此 4.1 完成不代表当前用户搜索已经自动写入 Checkpoint。

每个已准入并真正进入业务处理的搜索属于后续采集范围，但只进入有限滚动窗口：普通结构化
Checkpoint 保留 30 天，普通图片保留 3 天，失败图片保留 7 天；不存在永久保留的搜索记录。
4.2 必须先闭环真实 Store、可信服务端时钟 TTL、全部容量闸门和周期 apply 清理；通过后由 4.3
接入 A2、4.4 接入 A3。闭环前不得接入 A2/A3 全量采集。

## 角色分工

| 组件 | 回答的问题 | 权威边界 |
| --- | --- | --- |
| Trace | 哪次网络操作在何时经过哪里、结果如何 | 一次 HTTP 操作的时间线入口 |
| Checkpoint | 该阶段基于什么结构化结果做出决定 | 一次逻辑搜索的不可变中间证据 |
| Artifact | 必要时查看哪张短期原图、裁图或 overlay | 受 owner/TTL/audit 保护、可被多个兼容 Checkpoint 引用的单份二进制证据 |
| Task State | 用户现在可以做什么 | 唯一动作授权来源 |
| Response Store | 用户实际收到哪个可评分结果 | 最终公共投影和反馈绑定权威 |

Checkpoint 不是可执行快照。V1 不能直接反序列化为运行状态，也不能凭 Checkpoint 开放按钮或
自动续跑。

## 标识和拓扑

| 字段 | 规则 |
| --- | --- |
| `checkpoint_id` | 服务端生成 `ckpt_<32 lowercase hex>` |
| `artifact_id` | 服务端生成 `art_<32 lowercase hex>` |
| `trace_id` | 必填，必须是产生该结果的 `trace_<32 lowercase hex>` |
| `request_id` | 可选兼容字段，格式为 `req_<32 lowercase hex>`，不能作为内部唯一关联 |
| `identity_key` | 必填的安全身份键，不能为空；与 session、workflow 一起构成所有权边界 |
| `workflow_search_id` | 必填，一张上传页或 standalone A2 的逻辑父任务 |
| `search_id` | child scope 必填；standalone A2 与 workflow ID 相等 |
| `unit_id` | A3 crop 阶段必填；整页阶段禁止；A3 子题可选 |
| `workflow_task_revision` | 必填，A3 父 workflow 的 `task_revision`；standalone A2 与 `task_revision` 相等 |
| `task_revision` | 必填，当前 scope 的 revision；workflow scope 等于父 revision，child scope 表示子 A2 revision |
| `candidate_generation` | 候选相关 child 阶段可保存 `task_revision:candidate_revision`，首段只校验子 A2 `task_revision` |
| `predecessor_checkpoint_id` | 可选因果前驱，可跨 Trace 连接同一逻辑搜索的后续结果 |

一次逻辑搜索可以跨多个 Trace。诊断先以 Trace 找到当前 Checkpoint，再用业务 ID、revision 和
前驱关系查看同一搜索的其他 Checkpoint。

workflow scope 的两个 revision 必须相等；A3 child 同时保留父 `workflow_task_revision` 和子
`task_revision`，二者允许不同；standalone A2 因父子 ID 相等且没有 `unit_id`，两个 revision
也必须相等。不得用父 revision 校验子 `candidate_generation`。

Trace 若在后续批次增加 `checkpoint_id`，只能引用已经成功提交的 Checkpoint，不得把
`artifact_id` 或业务详情复制进 Trace。Trace 写入丢失时仍可按 Checkpoint 自带的
`trace_id` 反查。

## Envelope

```json
{
  "contract": "intermediate_checkpoint",
  "schema_version": 1,
  "checkpoint_id": "ckpt_...",
  "trace_id": "trace_...",
  "request_id": "req_...",
  "stage": "question_analyzed",
  "outcome": "success",
  "occurred_at": "2026-09-04T08:00:00+00:00",
  "expires_at": "2026-10-04T08:00:00+00:00",
  "retention_class": "normal",
  "input_fingerprint": "<sha256>",
  "owner": {
    "scope": "child_task",
    "session_key": "<sha256>",
    "identity_key": "invite_...",
    "workflow_search_id": "search_...",
    "search_id": "search_...",
    "unit_id": "unit_...",
    "workflow_task_revision": 5,
    "task_revision": 2,
    "candidate_generation": ""
  },
  "producer": {
    "code_revision": "<full git sha>",
    "component": "a3_unit_analysis",
    "component_version": "v1",
    "model_provider": "qwen",
    "model_name": "qwen-vl-max",
    "prompt_sha256": "<sha256>",
    "input_schema_version": "a3-unit-analysis-v1",
    "policy_version": "chapter-guard-v1",
    "data_version": "bank-manifest-v1"
  },
  "predecessor_checkpoint_id": "ckpt_...",
  "failure": null,
  "result": {},
  "artifacts": [
    {"artifact_id": "art_...", "role": "question_crop", "ordinal": 1}
  ]
}
```

Envelope 和嵌套结果在构造时深拷贝并冻结；序列化返回独立 JSON 副本。单条 `result` 最大
64 KiB，嵌套深度最大 7，通用集合最大 50 项，单段文本最大 2000 字，识别/证据摘录最大
1000 字。

`input_fingerprint` 是 `compute_input_fingerprint_v1` 返回的 digest，算法固定为：

1. 输入必须是已注册 `stage`、精确的 `CheckpointOwnerV1`、精确的 `ProducerVersionV1`，以及
   1～50 个 `input_digests`；
2. 每个输入名必须匹配 `[a-z][a-z0-9_]{0,63}` 且不能命中敏感字段黑名单，每个值必须是该
   阶段实际消费输入的 64 位小写 SHA-256 digest；
3. 组成对象 `contract="intermediate_checkpoint"`、`schema_version=1`、`stage`、`owner`、
   `producer`、`input_digests`，其中 `input_digests` 先按名称排序；
4. 用 `ensure_ascii=true`、递归 `sort_keys=true`、分隔符 `,`/`:` 的无空白 canonical JSON
   编码为 UTF-8，再计算 SHA-256，输出 64 位小写十六进制 digest。

原始输入内容不进入该对象。路径、原文件名、用户明文、时间、TTL、Trace/Request/Checkpoint
ID 也不得作为输入；相同业务输入在新的网络 Trace 中应得到相同 digest，stage、owner、输入
digest 名称或值、Producer 任一变化都必须改变结果。

## 阶段矩阵

| Stage | Scope | 正常结果段 | 图片角色 |
| --- | --- | --- | --- |
| `image_accepted` | workflow | `image_metadata` | 成功时 `source_page` |
| `image_routed` | workflow | `route_decision` | 可选 `source_page` |
| `page_understood` | workflow | `page_summary`, `unit_results` | 可选 `source_page` |
| `crop_prepared` | workflow + unit | `crop_geometry`, `crop_grounding` | 成功时 `question_crop`，可选 source/overlay |
| `crop_validated` | workflow + unit | `crop_validation` | 成功时 `question_crop`，可选 source/overlay |
| `question_analyzed` | child | `question_context`, `chapter_decision`, `load_observations`, `structure_decision` | 可选 source/crop |
| `coarse_search_completed` | child | `dimension_observations`, `candidate_counts`, `filter_decisions`, `candidate_scores` | 可选 source/crop |
| `rerank_completed` | child | `rerank_policy`, `candidate_scores` | 可选 source/crop/candidate |
| `answer_prepared` | child | 按 outcome 使用 `selection`、`delivery` | `success` 时至少一个 `answer_image` |

失败 Checkpoint 可以只保存严格 `failure`，不要求伪造尚未形成的正常结果段。部分成功允许保存
已形成的结果和稳定失败信息。Artifact 复制失败不能阻止结构化 Checkpoint；此时结果应降为
`partial` 并通过安全 failure code 表达证据缺失。

`answer_prepared` 的 outcome 规则单独冻结：`success` 必须有 `selection`、`delivery` 和至少一个
`answer_image`；`partial` 必须有 `selection`、`delivery`，但允许答案 Artifact 不完整；
`no_match` 只要求 `delivery`，并同时满足四项不变量：`answer_artifact_count=0`、
`media_status=not_available`、`delivery_code=NO_MATCH`、不存在 `answer_image` Artifact 链接，同时
禁止伪造 `selection`；`failed` 只要求严格 `failure`，不要求正常结果段或答案 Artifact。

## 结果段字段

所有段都是 exact-shape：缺少必填字段或出现未注册字段都拒绝。下面的“可选”以外不接受其他
字段。

### 图片和路由

- `image_metadata`：`width_px`, `height_px`, `byte_size`, `mime_type`, `sha256`,
  `orientation_status`, `applied_rotation_degrees`；可选 `orientation_confidence`,
  `orientation_method`。
- `route_decision`：`route`, `decision_source`, `reason_code`；可选 `confidence`。

图片像素尺寸只出现在 `image_metadata`，不能与结构力学工程尺寸混为同一字段。
入口当前允许 JPEG、PNG、WEBP、GIF 和 BMP；`mime_type` 必须按实际校验或转码后的字节记录为
`image/jpeg`、`image/png`、`image/webp`、`image/gif` 或 `image/bmp`，不能只相信扩展名。

### 整页识别

- `page_summary`：来源 schema、page disposition、group/unit/searchable/diagram/unknown 数量，以及
  `stored_unit_count/units_truncated`；保存数必须等于 `unit_results` 长度、不得大于总 unit 数，
  截断标志必须与“保存数小于总数”一致。
- `unit_results`：最多 50 项；每项保存 unit/group、父子题号、显示标签、searchability、status、
  reason codes、diagram roles 和最多 1000 字的结构化识别文字摘录。

不保存完整模型返回、完整 OCR、unassigned 全文、reasoning 或任意 notes/evidence 原文。
运行时没有“整页最多 10 个 unit”的业务保证；11～50 个 unit 必须可完整表达，超过 50 时按
上述计数显式截断，不能静默把总题数改成 50。

### 裁图

- `crop_geometry`：unit、裁剪方法、模型 bbox、扩展 bbox、像素 bounds、源图与裁图像素尺寸。
- `crop_grounding`：schema、page/grounding status、reason codes、有界 binding evidence 摘录。
- `crop_validation`：schema、verdict、固定六项门禁、external-load status、reason codes。

`model_bbox` 和 `expanded_bbox` 可为 `null`；非空时必须是四个整数 `[x1,y1,x2,y2]`，并满足
`0 <= x1 < x2 <= 1000`、`0 <= y1 < y2 <= 1000`。像素 `bounds` 必须恰有
`left/top/right/bottom` 四个非负整数且严格有序，右/下边界不得超过源图尺寸；裁图宽高必须
分别精确等于 `right-left`、`bottom-top`，所有源图和裁图像素尺寸均为 1～1,000,000 的整数。
`unit_id` 还必须与 Checkpoint owner 一致。

`external_load_status` 只允许 `not_run/not_configured/yes/no/error`。`checks` 必须恰好包含
`selected_diagram_match/single_target_diagram/structure_complete/supports_complete/`
`external_loads_complete/image_clear`，每项为 boolean 或 `null`；仅六项全为 `true` 时
`verdict=verified`，否则必须为 `review_required`。非失败 outcome 组合固定如下：

| `verdict` | `external_load_status` | 允许 outcome | 含义 |
| --- | --- | --- | --- |
| `verified` | `yes` / `not_configured` | `success` / `partial` | 裁图可继续下行 |
| `verified` | `no` / `error` | `needs_input` | 外荷载门禁不通过或出错，转人工 |
| `review_required` | `not_run` | `needs_input` | 六项校验未通过，外荷载检查不再运行 |

`verified + not_run`，以及 `review_required` 搭配其他 external status，均为非法组合；
`failed` 可以只保存严格 failure，不伪造 `crop_validation`。

### 单题理解

- `question_context`：analysis schema、识别文字摘录、category。
- `chapter_decision`：章节、0..1 confidence、source、scope status、reason code；可选 evidence
  摘录和 topic ID。
- `load_observations`：每项只能包含 `type` 与 `raw`；type 只允许 `集中/均布/弯矩`，raw
  必须非空且最多 200 字。
- `structure_decision`：结构类型、来源、筛选是否适用、reason code；可选 confidence。

### 搜索和复筛

- `dimension_observations`：粗筛工具内部对查询题识别出的工程尺寸，每项包含 kind、raw、
  normalized、unit、source、status、稳定 `reason_code`，不能提前归到 `question_analyzed`。
  kind 初始支持 `long_width/single_side/span/member_length/other`。非 `failed` 粗筛不得用空列表
  表示“没有尺寸”：未运行时必须保存
  `{"kind":"other","raw":"","normalized":"","unit":"","source":"not_run","status":"not_run","reason_code":"DIMENSION_NOT_RUN"}`；
  已运行但未识别时使用同样的空值形状，并令 `source=dimension_recognizer`、`status=missing`、
  `reason_code=DIMENSION_NOT_FOUND`。
- `candidate_counts`：`chapter_scanned -> load_scored -> positive_score -> rerank_pool ->
  after_dimension_filter` 必须单调不增；同时必填粗筛明细自己的 `stored_score_count` 和
  `scores_truncated`，可选 `excluded_previous/remaining`。粗筛完成时尚未形成最终 `visible`，
  禁止在本段伪造该计数。
- `filter_decisions`：最多 20 项；保存 filter、applied/skipped/fallback/failed、before/after、
  reason code、policy version，after 不得大于 before。
- `candidate_scores`：最多 50 项；只保存不透明 candidate ID、粗筛/复筛排名与分数、final
  score、状态、reason code、结构和已有尺寸元数据。candidate ID 不得用绝对路径。
  `coarse_search_completed` 禁止每项出现 `visible`；`rerank_completed` 则要求每项必填布尔
  `visible`，并把所有实际展示候选放入明细。
- `rerank_policy`：是否实际复筛、输入/完成/失败数、最终 `visible`、复筛明细自己的
  `stored_score_count/scores_truncated`、阈值、全量显示阈值、fallback limit、是否回退、reason
  code 和 policy version。`visible` 必须是复筛/跳过/回退策略实际选出的用户可见候选数，不能
  倒填为粗筛池大小；它必须等于 `candidate_scores` 中 `visible=true` 的项数。

Checkpoint 只能保存最多 50 条候选明细，并以 `stored_score_count/scores_truncated` 明示是否
省略未展示候选。复筛截断只能省略 `visible=false` 的项，必须满足
`visible <= stored_score_count <= 50`；若实际展示数本身超过 50，Checkpoint 写入必须拒绝，
不能静默省略已展示候选或伪造较小的 `visible`。

粗筛 outcome 与计数矩阵固定为：

| outcome | `after_dimension_filter` | `stored_score_count` / 明细 | 其他约束 |
| --- | ---: | --- | --- |
| `success` / `partial` | `> 0` | `> 0`，且等于候选明细数 | 必须有至少一条尺寸 observation；候选项禁止 `visible` |
| `no_match` | `0` | `0`、空明细、`scores_truncated=false` | 必须有至少一条尺寸 observation |
| `failed` | 可不提供正常结果段 | 可不提供 | 必须保存严格 failure |

所有非失败粗筛还满足 `stored_score_count <= positive_score`、
`stored_score_count <= after_dimension_filter`，且 `scores_truncated` 当且仅当
`stored_score_count < after_dimension_filter`。

复筛 outcome 与 policy 矩阵固定为：

| outcome | `reranked` | `fallback_used` | `visible` | 完成/失败数 |
| --- | --- | --- | ---: | --- |
| `success` | `true` | `false` | `> 0` | `completed_count=input_count`、`failed_count=0` |
| `no_match` | 任意 | `false` | `0` | 若 `reranked=true`，必须完整完成且零失败 |
| `partial` | `false` | `true` | `> 0` | 服从通用计数约束 |
| `skipped` | `false` | `true` | `> 0` | `completed_count=failed_count=0` |
| `failed` | 可不提供正常结果段 | 可不提供 | 可不提供 | 必须保存严格 failure |

所有非失败复筛还满足 `completed_count + failed_count <= input_count`、
`visible <= stored_score_count <= input_count`，`stored_score_count` 等于候选明细数，
`scores_truncated` 当且仅当 `stored_score_count < input_count`；每条复筛候选必须显式保存
boolean `visible`，其中 `true` 的数量必须精确等于 policy 的 `visible`。

### 最终选择

- `selection`：candidate ID、选择时排名、candidate generation、选择来源。
- `delivery`：答案 Artifact 数、media status、delivery code；Response Store 已完成最终绑定时
  可附 `response_id`。

最终公共交付仍以 Response Store 为权威。Checkpoint 不得覆盖或伪造 response binding。
候选不存在、用户明确选择无匹配或答案文件无法形成可交付结果时，`answer_prepared` 可以是
`no_match`；它是一个可诊断的业务终态，不应伪装成带答案图片的 `success`。

## Failure

`outcome=failed` 必须提供：

```json
{
  "code": "RERANK_PROVIDER_FAILED",
  "kind": "external_model",
  "retryable": true,
  "fallback": "coarse_order",
  "last_successful_checkpoint_id": "ckpt_..."
}
```

只允许稳定 code/kind/fallback，不接受异常正文、类名加 message、traceback 或路径。失败记录不能
使用 `normal` retention class。

## Producer Version

每条 Checkpoint 必须保存完整 40 位 Git revision、组件名和组件版本。适用时再保存成对的模型
provider/name、Prompt SHA-256、输入 schema、策略版本和无路径的数据版本。模型 provider/name
必须同时为空或同时存在。

Producer 的组件、模型和版本类字段只能使用安全 symbol：以字母或数字开头，后续只允许字母、
数字、`_ . + -`，总长 1～128；必填字段不能为空。所有 envelope/result 文本还拒绝绝对路径、
以盘符开头的 drive-relative 路径（如 `C:private`）、URL、Bearer/`sk-...` 形态及
`sessionid/session_id/cookie/token/access_token/refresh_token/password/passwd/secret` 等敏感键的
`key=value` 或 `key:value` 形态。该规则不把普通结构力学符号当秘密：识别摘录中的 `P`、`L`
和 `P/L=2` 合法。保存 hash/版本是为了解释与有限复用，不保存 Prompt 正文、endpoint 或模型原文。

## Artifact Descriptor

Artifact 元数据必须包含：

- `artifact_id` 和自身的权威 owner 拓扑；
- SHA-256、字节数、MIME、像素宽高；
- `created_at/expires_at/retention_class`；
- `available/purged` 状态；purged 时必须有 `purged_at` 和稳定 purge reason。

Descriptor 不包含绝对路径、相对 storage key 或原文件名。物理位置只能由受控根目录和 Store
内部映射解析，访问时必须同时验证 ID、owner、状态和 TTL。

同一逻辑图片只创建一个 Artifact descriptor 和一份物理文件；多个 Checkpoint 通过相同
`artifact_id` 引用它，不能因为阶段不同再次复制。引用不延长 Artifact TTL；Artifact 到期后，
所有引用同时不可读取。不同 `session_key`、`identity_key` 或 `workflow_search_id` 之间禁止复用
同一个 descriptor，即使内容哈希相同；未来若做底层字节去重，也必须保留相互隔离的逻辑 owner
记录，不能借哈希获得跨 owner 访问。

Artifact owner 不要求与每个引用它的 Checkpoint owner 完全相等，而按 role 使用以下兼容矩阵；
所有规则都先要求 session、identity 和 workflow 相同：

| Role | Artifact owner | 允许引用的 Checkpoint |
| --- | --- | --- |
| `source_page` | workflow + `workflow_task_revision` | 同父 revision 的 workflow，或其 A3 child/standalone A2 child |
| `question_crop` / `crop_overlay` | workflow + `unit_id` + `workflow_task_revision` | 同 unit 的 crop 阶段，或由该 unit 下行的 child |
| `candidate_image` / `answer_image` | 完整 child owner | `search_id`、unit、父子 revision 及适用 generation 均兼容的 child |

跨 scope 引用只放宽上述 owner 形状，不放宽访问控制；无法证明 role、unit 或父子 revision 兼容
时读取必须 fail-closed。4.1 在此冻结矩阵，但当前 Checkpoint 与 Artifact descriptor 的独立纯
对象构造器不能完成双方 owner 联合校验；4.2 Store 解引用 `artifact_id` 时必须执行并测试该
授权规则，未通过不得返回元数据或图片。

角色初始为 `source_page/question_crop/crop_overlay/candidate_image/answer_image`。source、crop、
overlay 每个 Checkpoint 最多一个；candidate/answer 用从 1 开始的 ordinal 保持稳定顺序。
Artifact MIME 与上传契约一致，必须支持 JPEG、PNG、WEBP、GIF、BMP 的实际媒体类型。

## 保留与容量

| Class | Checkpoint 默认/上限 | Artifact 默认/上限 |
| --- | ---: | ---: |
| `normal` | 30/30 天 | 3/3 天 |
| `failed` | 30/30 天 | 7/7 天 |
| `feedback` | 30/365 天 | 30/365 天 |
| `investigation` | 30/90 天 | 30/90 天 |

所有记录创建时必须同时获得有限 `expires_at`。4.1 构造器只校验时间带时区、到期晚于创建且
间隔不超过 class 上限；它不读取当前系统时间。4.2 Store 必须以可信服务端当前时间生成、写入
和读取校验 TTL，不能信任客户端时间。反馈实际天数沿用控制库配置；调查延长必须显式审计，
不能设置永久 hold。

因此所有有效搜索只存在于有限滚动窗口中，而不是形成永久历史库。普通成功、`no_match` 和
正常跳过均使用 `normal`；明确 `failed` 使用 `failed`。Checkpoint 到期不等待 Artifact，
Artifact 到期也不因仍有 Checkpoint 引用而续期。

TTL 之外，运行配置必须显式提供七个正数：`max_checkpoint_rows`、`max_artifact_rows`、
`max_audit_rows`、`max_trace_rows`、`max_artifact_bytes`、`min_free_bytes`、
`max_artifacts_per_checkpoint`；最后一项还必须在 1～50。4.1 只冻结这些设置不可缺失；具体生产
容量在 4.2 根据搜索量、图片大小分布和磁盘余量确定，不能无证据写死。

`max_audit_rows` 只约束本证据链的 Checkpoint/Artifact 查看与维护审计，不包含 8795 控制库的
管理员审计。4.2 必须为证据审计定义有限保留/轮转规则，并为 `auto_purge` 预留可写空间，避免
“审计容量已满导致无法记录清理、因而无法清理”的闭锁；控制库管理员审计保持现有边界不变。

达到容量时先清理已过期且未 hold 内容。仍不足时，停止新增本次诊断证据但搜索继续，健康状态
必须 degraded 并增加有界安全计数。这里的“证据写入 fail-open”不等于丢弃容量限制，也不承诺
每次搜索必有 Checkpoint；不得为了保存证据重试或重复计费。

### 4.2 采集准入门

4.2 的首个交付必须先完成 Store 生命周期闭环，而不是先把九个阶段接入生产：

1. 由真实 Store 使用可信服务端当前时间创建并校验 TTL，过期后读取 fail-closed；
2. 写入前执行 Checkpoint、Artifact、证据审计、Trace 行数，Artifact 总字节数、单 Checkpoint
   Artifact 数量和最小剩余磁盘空间闸门；
3. 安装并验证周期 retention plan/apply，能清理过期 Checkpoint、Artifact、本证据链到期审计、
   孤儿和批准范围内的 Trace 证据，保留清理审计写入余量并报告实际释放结果；
4. 用非 A2/A3 全量流量的受控样本验证过期、容量拒绝、重试、审计和恢复。

上述四项未闭环时，不得开启 A2/A3 全量 Checkpoint/Artifact 采集。默认 dry-run、只有计划没有
apply、或只有 TTL 字段没有周期执行，都不算通过准入门。

### 4.3 / 4.4 运行时采集责任

4.2 只交付生命周期和容量控制面，不等于已经拥有完整业务证据。4.3 才负责从 A2 的实际 runtime
和工具结果组装完整、白名单化的单题阶段摘要，包括上传、题目理解、粗筛、尺寸过滤、复筛、选择、
交付和稳定失败；不得只保存 Agent 最终状态或不完整 Trace 摘要。4.4 再接 A3 整页理解、unit、
裁图、外荷载校验及父子关联。同一逻辑搜索只 best-effort 保存它实际到达的阶段，不补造未运行
阶段，也不因证据失败阻断业务。

## 审计动作

V1 注册五种动作：`view_checkpoint`、`view_artifact`、`extend_retention`、`delete_evidence`、
`auto_purge`。审计只保存 actor/owner 的安全 ID、目标内部 ID、动作、结果码、时间和新期限；
不保存图片、识别文字、路径或完整 Checkpoint。

查看 Checkpoint 或 Artifact 必须先成功提交对应查看审计；审计无法提交时不得返回记录或图片，
即查看 fail-closed。后台自动 purge 的审计故障必须进入健康告警和重试。管理员主动延期或删除
在审计无法提交时同样 fail-closed。

## 与 Trace 的一致性

1. Checkpoint 成功提交后，后续批次才可写仅含 `checkpoint_id` 的 Trace 关联事件/字段。
2. Trace 写入继续 fail-open；其失败不能回滚已形成的业务结果或触发重复计算。
3. Checkpoint 自带 `trace_id`，因此 Trace 关联事件丢失不会令记录失去来源。
4. Trace 不包含 `artifact_id`、识别内容、分数列表、图片哈希或 retention hold。
5. health/login/media GET 等 Trace 不创建 Checkpoint。

## 安全失败语义

- 写 Checkpoint/Artifact、证据队列满、容量不足：业务 fail-open；
- Checkpoint/Artifact 查找、schema/decode、所有权、TTL、状态或路径解析失败：读取 fail-closed；
- Checkpoint 复用时版本、owner 或 `input_fingerprint` 不一致：复用 fail-closed，重新计算；
- 查看审计无法成功提交：查看 fail-closed，不返回证据；
- Response finalization：沿用现有 fail-closed；
- retention plan/hash/backup/path/drift 校验：维护 fail-closed；
- 不论哪种失败，都不能把任意异常文本写入 Checkpoint、Trace、公共输出或审计。

## 4.1 验收门

- 九个阶段、scope/outcome、required sections 和 Artifact role 使用一个可执行冻结矩阵；
- 所有阶段至少有一个 canonical success fixture；失败、partial、no-match 和 needs-input 的结构可
  由同一 envelope 表达；
- 父子 ID、unit、父子 revision、candidate generation、输入指纹和前驱 ID 非法时拒绝；
- result 深冻结、exact-shape、大小/深度/列表有界；
- 路径、URL、敏感 key、完整 Prompt/模型原文/traceback 不能进入 result；
- 荷载类型、bbox、裁图门禁、候选计数单调性、分数范围和候选唯一性有契约测试；
- `page_summary` 可显式表达超过 10 个 unit，保存数和截断语义一致；
- 粗筛不伪造 `visible`，查询题工程尺寸属于粗筛，复筛独立保存最终可见数及明细截断语义；
- `answer_prepared=no_match` 不要求 selection 或答案 Artifact，且不能冒充成功；
- Artifact descriptor 的 owner/hash/大小、JPEG/PNG/WEBP/GIF/BMP、像素、TTL 和 tombstone
  基础属性有本轮契约测试；跨 scope role 矩阵和单份多引用语义在 4.1 冻结，由 4.2 Store
  解引用时强制实现并测试；
- 3/7/30/90/365 天上限和七项显式容量策略有契约测试；
- 4.1 的 33 项契约测试只证明纯数据形状与不变量，不证明真实 Store、可信时钟、周期清理或
  A2/A3 runtime 采集已经实现；
- 不修改 Trace、Response、Task State、A2/A3 runtime 或任何服务入口。
