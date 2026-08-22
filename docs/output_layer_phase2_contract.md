# 题库 Agent 输出层升级：阶段 2 输出契约与策略

## 1. 契约结论

输出层 V1 采用**确定性目录渲染**，不采用“接收任意字符串后清洗”，也不新增模型调用。

```text
业务状态 / 工具五态 / 传输错误
              ↓
 message_key + protocol + 白名单 facts + allowed_actions
              ↓
        确定性用户输出目录
              ↓
 text + protocol + allowed_actions
              ↓
    普通 JSON / 流式事件 / 浏览器展示
```

核心原则：

- 业务层决定事实、状态和允许动作；
- 输出层只验证并表达，不改变状态，不执行动作；
- 已审核固定话术由稳定 `message_key` 找到并原样输出；
- 动态话术只允许插入登记过的 facts；
- 未登记或验证失败时 fail closed，使用与协议状态/动作一致的安全兜底；
- 原始异常、工具 `error`、HTTP `detail`、模型原文、路径和堆栈不是合法 facts；
- 同一语义结果在普通和流式渠道的公开字段完全一致。

## 2. 与现有模块的职责关系

| 模块 | 保留职责 | 不再承担的职责 |
| --- | --- | --- |
| `ToolResult` | 工具五态、内部 code、结构化 data、重试属性 | 不直接提供最终用户文案 |
| `RequestProtocol` | 请求状态、层级、稳定 code、主恢复动作、请求关联 ID | 不单独决定完整文案，也不承载内部异常详情 |
| `AgentState` / `A3SessionState` | 当前业务事实和允许动作 | 不拼接最终字符串 |
| `render.py` 等现有渲染器 | 作为首批审核文案和模板来源 | 不接受任意 error 字符串 |
| `safe_answer_v0` / `reply_shell_v2` | 零工具对话和拒绝动作 | 不代替业务结果输出层 |
| A3 Runtime | 组合父 A3 与子 A2 的结构化结果 | 不覆盖/拼接子 `response.text` |
| FastAPI | 状态码、Cookie、JSON/NDJSON 包装 | 不把异常字符串翻译成业务文案，不推测动作 |
| 浏览器 | 展示服务端结果，处理浏览器本地错误 | 不重写已带协议的服务端文案，不从 raw detail 推断文案/动作 |

输出层建议在后续实现为一个独立纯模块，例如 `tiku_agent/user_output.py`，文案目录可拆为 `user_output_catalog.py`。名称可以调整，但不能把它并入状态机或 FastAPI 异常处理器内部。

## 3. 核心数据契约

以下是语义契约，不要求阶段 3 必须逐字使用这些类名。

```python
@dataclass(frozen=True)
class FinalOutputRequestV1:
    schema_version: int                 # 固定为 1
    kind: FinalOutputKind               # result | transport_error | client_error
    message_key: str                    # 稳定的用户语义键
    protocol: RequestProtocol           # 现有五态协议
    phase: str                          # 当前业务阶段，只用于校验
    facts: Mapping[str, JsonValue]      # 仅允许本 message_key 声明的字段
    allowed_actions: tuple[UserAction, ...]
    notice_keys: tuple[str, ...] = ()   # 只允许登记过的 PARTIAL/降级提示
    bounded_text: str = ""              # 仅给明确登记的受约束模型特例


@dataclass(frozen=True)
class ProgressOutputRequestV1:
    schema_version: int
    progress_key: str
    request_id: str
    search_id: str
    sequence: int
    facts: Mapping[str, JsonValue]


@dataclass(frozen=True)
class PublicMessageV1:
    schema_version: int
    kind: OutputKind
    message_key: str
    text: str
    protocol: RequestProtocol | None     # progress 为 None
    allowed_actions: tuple[UserAction, ...]
    request_id: str
    search_id: str
    sequence: int | None                 # 仅 progress 使用
```

`UserOutputRequestV1` 可作为上述 final/progress 两种请求的联合类型。这样 progress 不需要伪造一个尚未发生的最终 status/action，同时所有公开消息仍携带同一 request/search ID。

### 3.1 为什么必须有 `message_key`

`RequestProtocol.code` 和用户表达不是同一粒度：

- 多个 A3 澄清场景都可能是 `CLARIFICATION_REQUIRED`，但问题和动作不同；
- `ToolResult` 的 code 很细，例如不同模型/筛选退化，用户可能只需要同一个安全提示；
- 同一个 `NO_MATCH` 在章节检索、全局检索、候选耗尽和答案文件缺失时，下一步不同。

因此：

- `code` 回答“机器上发生了什么”；
- `message_key` 回答“应该怎样向用户表达”；
- 二者的允许组合在目录中注册，不能随意拼配。

这里的 `message_key` 同时承担“本次公开业务事件”和“审核文案目录键”的职责。例如候选已经就绪、答案已经交付、等待题号选择是三个不同事件。若以后确实出现同一事件需要多个独立文案版本，再拆分 `event_key/template_key`；V1 不先增加一层没有实际用途的抽象。

### 3.2 `message_key` 命名

建议使用稳定的小写点分层命名：

```text
conversation.greeting
search.chapter.required
search.candidates.ready
search.no_match.chapter
search.answer.ready
page.selection.required
page.crop.rejected
page.completed
system.queue.full
system.upload.decode_failed
system.service.unavailable
progress.search.chapter
```

键名不能包含章节、题号、供应商或模型名。动态内容全部放在 facts。

### 3.3 `kind`

| kind | 用途 | 是否可改变协议 |
| --- | --- | --- |
| `result` | A2/A3 最终业务回复 | 否 |
| `progress` | 排队、识别、检索、裁图等中间状态；使用独立 `ProgressOutputRequestV1` | 不产生最终 status/action |
| `transport_error` | 登录、上传、队列、额度、网络、反馈等服务端边界 | 使用对应协议，不改业务状态 |
| `client_error` | 浏览器本地解码、网络断开、超时 | 由同一 code 目录产生本地协议和文案 |

渠道（普通 JSON、NDJSON、浏览器）不是业务 kind。渠道只能改变外层封装，不能选择另一套文案。

## 4. 白名单 facts

### 4.1 首批允许字段

| facts 字段 | 类型与约束 | 权威来源 | 用途 |
| --- | --- | --- | --- |
| `chapter_name` | 1–40 字符的公开章节/题型显示名 | 章节目录解析结果 | 章节提示、章节无匹配 |
| `supported_chapters` | 最多 7 个已登记显示名 | `chapter_catalog` | 不支持范围提示 |
| `question_count` | 非负整数，设置合理上限 | A2/A3 状态 | 多题识别 |
| `candidate_count` | 非负整数 | 当前 candidate generation | 候选提示 |
| `remaining_count` | 非负整数 | A3 当前 revision | 当前题完成后的剩余题 |
| `ready_count` | 非负整数且不大于请求数 | A3 自动裁剪结果 | 批量准备结果 |
| `manual_count` | 非负整数且不大于请求数 | A3 自动裁剪结果 | 人工裁剪提示 |
| `question_label` | 1–40 字符公开标签；非法时退回 `图片第 N 题` | A3 稳定 page index + 已验证 label | 选题、裁剪、答案 |
| `source_chapters` | 最多 7 个已登记显示名 | 全局检索结果 | 全局候选来源 |
| `continuation_available` | 布尔值 | 状态机 | 是否可以“继续搜” |
| `global_search_offered` | 布尔值 | 状态机 | 是否可以建议全局搜索 |
| `active_image_preserved` | 布尔值 | Session/Artifact 状态 | 是否允许“直接重试” |
| `delivered_image_count` | 非负整数 | 媒体持久化后的实际结果 | 答案/候选是否真正交付 |
| `retry_after_seconds` | 非负整数，有上限 | 队列/HTTP 策略 | 等待提示（若文案需要） |

### 4.2 明确禁止字段

下列内容不得进入 `facts`，也不得通过换名规避：

- `exception`、`error_message`、`traceback`、`stack`；
- 任意工具 `error` 原文；
- `HTTPException.detail` 原文；
- 模型 raw output、reasoning、schema 原文；
- 本地路径、数据库路径、URL、端口、模型 endpoint；
- API key、token、邀请码、Cookie、管理员信息；
- 内部 prompt、route code、reason code、置信度和调试说明；
- 未限制长度的 `title_text`、`visible_text`、`context_text`；
- 任意名为 `message`、`text`、`detail`、`reason` 的通用字符串槽。
- 完整候选记录、相似度/模型 score、session ID 或本地媒体路径。

内部诊断应通过 request/search/task ID 在服务端日志中关联，不能随 `UserOutputRequestV1` 传给渲染器。

### 4.3 动态标签规则

A3 的题目标签部分来自模型解析，目前只有类型检查和 `strip()`。对外使用前必须：

1. 优先使用代码生成的稳定 `page_index`；
2. 模型标签只允许短题号样式，不允许句子、控制字符或标记语法；
3. 验证失败时使用 `图片第 {page_index} 题`；
4. 选择解析和展示使用同一公开标签，不能一个被清洗、另一个仍使用原值。

## 5. 动作契约

### 5.1 区分协议主动作与完整允许动作

现有 `RequestProtocol.action` 保留，作为兼容字段和主要恢复动作；新增 `allowed_actions[]` 表示当前状态真实允许的全部用户动作。

建议首批动作：

```text
upload_image
retry_upload
retry_request
retry_search
retry_current_stage
select_question
prepare_units
crop_question
select_candidate
show_candidates
continue_search
change_chapter
global_search
cancel_current_question
finish_page
new_chat
relogin
retry_feedback
contact_author
```

名称最终可按现有前端 action 统一，但必须满足：

- 动作由状态机、业务服务或边界策略提供；
- 输出层不能因为某句话“听起来自然”就新增动作；
- FastAPI 和浏览器不能根据 `has_active_image`、错误文字或 `retryable` 自行追加动作；
- `RequestProtocol.action` 非空时，必须属于 `allowed_actions` 的协议兼容映射；
- 按钮、文字提示和协议动作必须来自同一份动作集合。

实现时可以复用 `action_permissions_v2.py` 的授权矩阵思想，但不能直接拿 SafeConversationContext 代替本契约：它不包含题数、实际媒体交付、PARTIAL notice 等输出事实。

### 5.2 状态—动作不变量

| status | 最低要求 |
| --- | --- |
| `SUCCESS` | 可以没有下一步；若文案提到继续/选择/上传，对应动作必须存在 |
| `NEEDS_INPUT` | 至少有一个当前可执行的输入动作，或明确标记为不可继续的终止结果 |
| `NO_MATCH` | 至少给出一个业务层允许的换章节、重新上传、返回候选或联系作者等动作 |
| `PARTIAL` | 必须有实际可用结果；`retryable=true` 时给出相应重试动作 |
| `ERROR` | 不宣称产生候选/答案；`retryable=true` 时必须有重试动作，反之不得暗示重试一定有效 |

每个文案目录项声明 `mentioned_actions`。测试必须验证：

```text
mentioned_actions ⊆ allowed_actions
```

这比从最终中文里猜“重试”“换章节”可靠。

额外不变量：

- 文案说“题图已保留”时，`active_image_preserved=True`；
- 文案说“答案已发送/重发”时，`delivered_image_count > 0`；
- 文案说“继续搜”时，`continuation_available=True`；
- 文案说“全局搜索”时，业务层已开放 `global_search`；
- 文案和结构化 payload 同时提供“联系作者”时，授权开关和 contact 结构必须都存在；
- 保留的工作流 `phase` 与本次请求 `status` 是两条轴：历史 phase=ERROR 时，一次纯寒暄仍可以是 SUCCESS；
- 没有可交付结果的 PARTIAL 必须正规化为 ERROR，不能只因为工具返回了 PARTIAL 枚举就称“已返回部分结果”。

## 6. 文案目录契约

每个目录项至少包含：

```python
CatalogEntry(
    message_key="search.candidates.ready",
    allowed_statuses={SUCCESS, PARTIAL},
    allowed_codes={"COARSE_CANDIDATES_FOUND", "RERANK_COMPLETED", ...},
    required_facts={"candidate_count"},
    optional_facts={"question_label"},
    mentioned_actions={"select_candidate"},
    renderer=render_candidates_ready,
    max_chars=160,
)
```

### 6.1 渲染优先级

1. 找到 `message_key`，校验 protocol、facts、phase 和 actions；
2. 使用目录中的审核固定文本或确定性模板；
3. 对明确登记的 `bounded_text` 特例执行专用校验；
4. 任一步失败，记录内部 `output_contract_violation`；
5. 根据已验证的 status 和允许动作进入安全兜底；
6. 若 protocol 本身也矛盾，使用 `system.service.unavailable`，不尝试猜业务事实。

禁止的优先级：

```text
existing_text → 简单清洗 → 原样放行
```

因为输出层无法从字符串本身证明它是“已有固定话术”还是异常原文。固定话术必须通过已登记的 key 识别。

### 6.2 注册 notice，而不是拼接任意提示

现有 PARTIAL 通过 `ToolResult.error` 或 `rerank_note` 拼接“提示：……”。V1 改为 `notice_keys`：

```text
notice.multi_detection_fallback
notice.multi_crop_partial
notice.structure_filter_skipped
notice.rerank_coarse_fallback
notice.media_partial
```

每个 notice 都是审核目录项，声明适用 code/status、是否仍有可用结果以及提到的动作。渲染器可以把一个主 message 与若干已登记 notice 组合；不得接收自由 note。重复 notice 去重，组合后的总长度仍需校验。

### 6.3 安全兜底

兜底只使用已经验证的 status/action，不能引用原始 detail：

| 条件 | 安全兜底原则 |
| --- | --- |
| `NEEDS_INPUT + upload/retry_upload` | 说明需要补充或重新上传题图 |
| `NEEDS_INPUT + change_chapter` | 说明需要章节信息 |
| `NEEDS_INPUT + select_*` | 说明需要选择当前有效的题目/候选 |
| `NO_MATCH + change_chapter/upload` | 说明当前范围无可靠结果，并只列已允许动作 |
| `PARTIAL` | 说明已返回当前可用结果、部分检查未完成 |
| `ERROR + retry_search` | 说明本次检索未完成；只有活动题图确实保留时才说可直接重试 |
| `ERROR + retry_request` | 说明服务暂时未完成请求，可重新提交 |
| 无法验证 protocol/action | 只说“这次请求没有完成”，记录内部告警；不编造恢复动作 |

兜底文本同样是目录项，不在异常处理器中临时拼写。

### 6.4 受约束模型文本唯一特例

生产全流程当前可能为 A1 图片预检生成说明。V1 不新增模型调用，允许暂时保留该既有能力，但必须满足：

- `message_key` 明确登记为 `triage.a1.bounded_explanation`；
- 模型只能看到业务 handoff，不得看到异常、配置或诊断；
- 长度、字符、内部 route/schema/debug 词、等待承诺均被拒绝；
- 必须包含状态机允许的重新上传动作；
- 不得改变 A1 路由事实；
- 验证失败使用固定 `triage.a1.fallback`；
- `reply_source/fallback_reason` 只进日志，不进入用户 payload。

safe-answer 的受约束模型回复继续使用自己的事实校验，但最终也应以登记 key 进入公共输出契约。

## 7. 首批策略矩阵

### 7.1 A2 / 通用业务

| message_key 家族 | status | 必要 facts | 典型 allowed_actions | 现有基线 |
| --- | --- | --- | --- | --- |
| `conversation.*` | SUCCESS | 可选的当前能力状态 | upload/select/continue 中由状态决定 | `safe_answer_reply_v0.py`、`reply_shell_v2.py` |
| `search.upload.required` | NEEDS_INPUT | 无 | upload_image | “先发一张题图”类固定回复 |
| `search.chapter.required` | NEEDS_INPUT | `supported_chapters` 可选 | change_chapter；只有已开放时才 global_search | `render_chapter*_prompt` |
| `search.chapter.unsupported` | NEEDS_INPUT | 公开题型名、支持章节 | change_chapter / upload_image | `render_chapter_scope_unsupported` |
| `search.questions.ready` | SUCCESS | question_count | select_question | `render_multi_question_list` |
| `search.candidates.ready` | SUCCESS/PARTIAL | candidate_count，可选 question_label | select_candidate、按状态 continue_search | `render_candidates` |
| `search.candidates.rejected` | SUCCESS/NO_MATCH | continuation_available | continue_search/change_chapter/contact_author 中已允许项 | `render_candidates_rejected` |
| `search.no_match.chapter` | NO_MATCH | chapter_name | change_chapter/upload_image/contact_author 中已允许项 | `render_no_match` |
| `search.no_match.global` | NO_MATCH | 无 | change_chapter | `render_global_no_match` |
| `search.answer.ready` | SUCCESS | delivered_image_count > 0 | show_candidates/upload_image 等状态动作 | `render_answer` |
| `search.answer.missing` | NO_MATCH | 无 | show_candidates/select_candidate | 当前答案缺失固定语义 |
| `search.partial.*` | PARTIAL | 实际结果 + code 对应的布尔/计数事实 | 由状态提供 | 当前 partial note 需按 code 迁移 |
| `search.failed.retryable` | ERROR | active_image_preserved | retry_search 或 retry_request | 当前 `render_error` 改为固定映射 |

首批 tool code 归一化规则：

- `CHAPTER_REQUIRED / UNKNOWN_CHAPTER` → 章节输入模板；
- `NO_COARSE_CANDIDATES` → 当前章节无匹配；
- `NO_RELIABLE_RERANK_CANDIDATES` → 无可靠复筛结果；
- `NO_GLOBAL_*` → 全局无匹配；
- `ANSWER_FILES_NOT_FOUND` → 答案缺失并保留候选选择；
- `IMAGE_ANALYSIS_FAILED / MULTI_DETAIL_FAILED / COARSE_SEARCH_FAILED / RERANK_FAILED / GLOBAL_SEARCH_FAILED / ANSWER_LOOKUP_FAILED` → 对应固定失败类别，是否提示重试完全由真实 protocol 和资源事实决定；
- `BANK_ROUTE_FAILED` 当前不可重试，不得被 `_fail()` 自动升级为 retry_search；
- `MULTI_DETECTION_FALLBACK / MULTI_CROPS_UNAVAILABLE / STRUCTURE_*_FALLBACK / RERANK_*_COARSE_FALLBACK` → 注册 notice，不读取工具 error；
- `GLOBAL_RERANK_INCOMPLETE` 若没有可交付结果，应正规化为 ERROR；只有实际保留可用结果时才是 PARTIAL。

### 7.2 A3 整页流程

| message_key 家族 | status | 必要 facts | 典型 allowed_actions |
| --- | --- | --- | --- |
| `page.selection.required` | NEEDS_INPUT | question_count / remaining_count | select_question、prepare_units、finish_page |
| `page.units.prepared` | SUCCESS/PARTIAL | question_count、ready_count、manual_count | select_question |
| `page.crop.required` | NEEDS_INPUT | question_label | crop_question、cancel_current_question、finish_page |
| `page.crop.rejected.*` | NEEDS_INPUT | question_label 可选、固定 reason enum | crop_question |
| `page.namespace.clarification` | NEEDS_INPUT | 无 | select_question/select_candidate，由当前 phase 决定 |
| `page.cancel.scope_required` | NEEDS_INPUT | 无 | cancel_current_question/finish_page/continue_current 中实际项 |
| `page.unit.completed_remaining` | SUCCESS | question_label、remaining_count | select_question/finish_page |
| `page.completed` | SUCCESS | 无 | upload_image/new_chat 中实际项 |
| `page.stale.*` | NEEDS_INPUT | remaining_count 可选 | 当前有效的 select/upload 动作 |
| `page.failed.retryable` | ERROR | active_image_preserved | retry_current_stage/upload_image |

A3 父子组合必须在渲染前完成：子 A2 提供 `candidate_count/answer delivered/partial/error`，父 A3 提供 `question_label/remaining_count/page phase`。组合器选择最终 `message_key` 和 facts；不得先渲染子文本再覆盖或拼接。

### 7.3 传输和浏览器本地错误

| code 家族 | message_key | status/action 原则 |
| --- | --- | --- |
| `LOGIN_* / INVITE_*` | `system.login.*` | NEEDS_INPUT + relogin |
| `*_QUOTA_EXCEEDED` | `system.quota.*` | NEEDS_INPUT；无权继续时不添加重试 |
| `QUEUE_FULL / QUEUE_TIMEOUT` | `system.queue.*` | ERROR + retry_request，可带 retry_after |
| `UPLOAD_*` | `system.upload.*` | NEEDS_INPUT/ERROR + retry_upload，按现有协议 |
| `NETWORK_UNAVAILABLE / REQUEST_TIMEOUT` | `system.network.*` | ERROR + retry_request |
| `SESSION_EXPIRED / STALE_*` | `system.session.*` | NEEDS_INPUT + new_chat/当前有效动作 |
| `MEDIA_*` | `system.media.*` | ERROR/PARTIAL；实际资源缺失不能仍宣称完整成功 |
| `FEEDBACK_*` | `system.feedback.*` | NEEDS_INPUT/ERROR + retry_feedback（仅可重试时） |
| 未处理服务异常 | `system.service.unavailable` | ERROR + retry_request |

## 8. Progress 契约

Progress 是用户输出，不能继续接受任意 `message`。

建议调用形式：

```python
progress(
    progress_key="progress.search.chapter",
    facts={"chapter_name": "4力法"},
    request_id="req_...",
    search_id="search_...",
    sequence=3,
)
```

首批登记 key：

```text
progress.queue.waiting
progress.queue.started
progress.image.triage
progress.image.analysis
progress.search.chapter
progress.search.global
progress.page.understanding
progress.page.reunderstanding
progress.page.auto_grounding
progress.page.crop_validating
progress.page.unit_analysis
progress.page.auto_crop_ready
```

约束：

- 上游只能发 key + facts，不能发公开 message；
- stage 值从 key 派生或在目录注册，不能任意输入；
- 所有事件携带同一 request/search ID 和单调 sequence；
- progress 不承诺最终成功，不说“已找到”或“已通过”，除非对应事实已经提交；
- 普通接口可以不展示 progress，但流式展示的文字必须由同一目录生成。

## 9. 渠道一致性与最终定稿时机

### 9.1 规范输出

服务端先得到一个规范 `PublicMessageV1`：

```json
{
  "schema_version": 1,
  "message_key": "search.candidates.ready",
  "text": "我从题库里找到了 3 道比较像的题，你看看有没有想要的。",
  "status": "SUCCESS",
  "layer": "tool",
  "code": "RERANK_COMPLETED",
  "retryable": false,
  "action": "",
  "allowed_actions": ["select_candidate"],
  "request_id": "req_...",
  "search_id": "search_..."
}
```

传输方式只能这样包裹：

```text
普通 JSON：PublicMessage
流式 result：{type: "result", data: PublicMessage}
流式 error： {type: "error",  data: PublicMessage}
```

不能让流式 error 只放 `message`，也不能让前端再按 HTTP 状态重写业务文字。

### 9.2 媒体之后定稿

候选和答案的最终输出必须在媒体持久化后定稿：

- 文案说“答案已发给你”时，`delivered_image_count` 必须大于 0；
- 若业务成功但部分媒体未交付，结果降为 PARTIAL，并使用对应固定文案；
- 若全部媒体失败，不能保持 SUCCESS；
- 反馈专用媒体失败不应改变普通业务成功，但应继续单独记录。

### 9.3 前端所有权

浏览器：

- 原样展示服务端 `text` 和 `allowed_actions`；
- 对未知服务端 code 忽略 raw `detail/message`，使用规范安全兜底；
- 只为浏览器本地发生的解码、网络、超时产生 `client_error`；
- 本地错误同样使用稳定 code、固定目录项和动作，不从异常字符串推断；
- 会话过期必须明确提示并给出 `new_chat`/`relogin`，不能只把状态栏改回“准备就绪”。

## 10. 协议强化要求

阶段 3–5 接入时应补强 `RequestProtocol`：

1. 通过注册 code 构造时，`status/layer/retryable/default action` 由注册表唯一决定；
2. `from_dict` 校验 code 与这些字段是否一致，不能接受矛盾组合；
3. 直接构造只保留给明确的内部动态 tool code，并经过统一校验；
4. API 保留 `schema_version`；
5. 每个入口沿用同一个 request ID，progress/result/log 不得各自新建；
6. `allowed_actions[]` 是新的完整动作来源，单数 `action` 仅作兼容；
7. 不在协议中增加异常详情字段。

## 11. 数据最小化要求

输出安全不只检查 `text`。当前 A3 session snapshot 还可能携带内部 intent reason/source/confidence、自动裁剪 reason code 等诊断字段。V1 要求：

- 公共 session payload 只保留前端实际需要的状态、公开标签和动作上下文；
- 模型 reason、fallback reason、内部 reason code、置信度、原始错误只留服务端日志；
- 前端未使用的内部字段不因“暂时没有展示”就继续公开；
- `title_text/context_text` 如确需展示，必须作为单独审核过的用户内容字段，限长并确认来源，不得混入模板 facts；
- 用户反馈快照继续遵守现有最小上下文与目标回复规则。

## 12. 安全与一致性验证矩阵

### 12.1 注入样本

每个入口都使用以下污染值测试：

```text
RuntimeError: invalid_observation_schema
C:\private\question.jpg
/srv/app/config.local.json
https://provider.example/v1?token=secret
Authorization: Bearer secret-value
{"error":{"type":"schema_error","raw":"..."}}
Traceback (most recent call last): ...
```

注入位置：

- ToolResult ERROR / NEEDS_INPUT / PARTIAL / NO_MATCH；
- state.last_error；
- HTTPException.detail；
- AgentProtocolError message；
- progress 来源；
- A1 bounded model reply；
- A3 intent reason 和 snapshot；
- 浏览器收到的未知 detail/event message。

期望：公开文本和公共 payload 都不包含污染值；日志仍可用 request/search ID 找到服务端诊断。

### 12.2 语义矩阵

每个关键场景验证：

```text
message_key
status / layer / code / retryable
action / allowed_actions
required facts
最终 text
普通 JSON 与流式逐字段一致
按钮与 allowed_actions 一致
状态机未被输出层修改
```

### 12.3 未知输入

- 未注册 message key：安全兜底 + 内部告警；
- 未声明 fact：拒绝渲染；
- 缺 required fact：安全兜底；
- 非法动态标签：使用稳定 page index 兜底；
- 文案目录声称未允许动作：测试失败；
- 协议字段矛盾：不渲染业务成功，使用服务异常兜底；
- 媒体实际交付与文案不符：降级或失败。

## 13. 阶段 2 已确定的设计决定

1. **不新增输出模型调用。**
2. **不把正则清洗当主要安全机制。**
3. **已有固定话术通过 message key 保护，不接受任意 existing text。**
4. **`message_key` 与 machine `code` 分离。**
5. **业务层提供 facts 和 allowed actions，输出层不得推断。**
6. **A3 父子流程在渲染前组合结构化事实。**
7. **progress 纳入统一输出边界。**
8. **普通 JSON、流式和浏览器共用同一语义输出。**
9. **媒体持久化后才最终确定成功/部分成功文案。**
10. **公共 payload 同样执行数据最小化，不只防 text 泄露。**
11. **现有 safe answer/reply shell 保持独立职责，最终接入公共出口。**
12. **本轮实现目标限定在 8790 的 A2/A3/Web/Stream，不改 8788、8795 和 CLI。**

## 14. 阶段 3 的最小进入条件

只有以下条件经审查确认后，才进入输出核心实现：

- 同意本轮生产范围；
- 同意 A/B/C 三类现有话术基线；
- 同意 `message_key + facts + allowed_actions`，而不是字符串清洗方案；
- 同意 progress 和公共 session payload 也属于输出安全边界；
- 同意媒体交付失败不能继续宣称完整成功；
- 同意现有 A1 模型说明只作为受约束特例保留，不新增模型调用；
- 首批 message key、facts 和动作命名可在实现中做机械微调，但不得改变上述职责与安全不变量。

阶段 3 应先实现纯函数目录、契约验证和对抗测试，不立即把 A2/A3 所有分支一次性迁移。
