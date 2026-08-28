# 统一任务状态快照 V1 契约（阶段 3.1 / 3.2.1）

## 结论与范围

阶段 3.1 完成父 workflow、当前子题任务和题目单元的权威状态契约定稿。本文件冻结 V1 的字段、词汇、字段来源、派生谓词、拓扑边界、一致性错误和冲突策略，是后续实现的权威规范。阶段 3.2.1 已实现从“已冻结 read-set + 可信入口证据”到 `TaskStateSnapshotV1` 的纯构造器，落在 `tiku_agent/task_state_builder.py`。

**阶段 3.2.2 的锁内读取与运行时接入尚未实现。** 当前 HTTP、stream、前端和既有 `session_snapshot()` 仍未提供本契约定义的统一快照；不得因为纯构造器已存在就声称阶段 3 已接入运行时。

以下内容不属于阶段 3.1：

- 阶段 4 的识图、裁图、章节、荷载、候选语义结果和图片 `artifact_id`；
- 阶段 5 的强父子外键、幂等键、执行锁、完整任务历史和通用状态版本；
- 阶段 6 的后台任务生命周期与 HTTP 解耦；
- 暂停/继续。

## 权威状态源与运行拓扑

| 层级 | 唯一当前状态源 | 权威身份 | 权威阶段与任务代次 |
| --- | --- | --- | --- |
| 父 workflow | `SQLiteA3SessionStore` 中的 `A3SessionState` | `workflow_search_id` | `phase`、父 `task_revision` |
| 当前子题任务 | `SQLiteSessionStore` 中的 `AgentState` | `current_search_id` | `phase`、子 `task_revision` |
| 当前题目单元 | 父状态中的 `units`、`selected_unit_id`、`completed_unit_ids`、`searched_unit_ids` 和 `auto_crops` | `(workflow_search_id, unit_id)` | 父 phase 与本文件定义的互斥 unit 状态 |

运行拓扑是构造器的可信配置，不得根据“父记录是否刚好存在”猜测：

- **A3 wrapper**：图片入口由 `A3Runtime` 包装，可能路由到 A1、direct A2 或 A3 多题流程；
- **standalone A2**：入口本身就是独立 `SessionRuntime`，不存在 A3 父 workflow。

以下数据不是当前状态源：

- `A3SessionState.current_search_id`：上传时可等于父 ID，direct A2 路由后又可能变为子 ID；
- 现有顶层 `session_snapshot.phase/search_id/task_revision`：会按路由和阶段切换父子语义；
- `AgentResponse.state`：描述一次响应的局部结果，不保证等于响应后父 workflow 状态；
- `response_store`：保存已交付回复的历史投影，回答“当时交付了什么”，不回答“现在到哪一步”；
- trace、task log 和费用账本：是事件或观测证据，不能倒推覆盖当前状态；
- 本地路径、URL、模型原文和异常正文：不得进入公共状态快照。

## V1 公共结构

```json
{
  "schema_version": 1,
  "workflow": {
    "exists": true,
    "workflow_id": "search_...",
    "kind": "IMAGE_SEARCH",
    "route": "A3",
    "task_revision": 7,
    "phase": "A2_ACTIVE",
    "status": "RUNNING",
    "completed_steps": [
      "IMAGE_ACCEPTED",
      "ROUTE_DECIDED",
      "PAGE_UNDERSTOOD",
      "UNIT_CATALOG_READY",
      "UNIT_SELECTED",
      "CHILD_TASK_STARTED"
    ],
    "allowed_actions": ["select_unit", "cancel_current_unit", "finish_page"],
    "next_stage": "FOLLOW_CHILD_TASK"
  },
  "active_child_task": {
    "task_id": "search_...",
    "kind": "A2_QUESTION",
    "unit_id": "g1-u2",
    "task_revision": 1,
    "phase": "WAIT_CANDIDATE_CHOICE",
    "status": "WAITING_USER",
    "completed_steps": [
      "QUESTION_ACCEPTED",
      "QUESTION_ANALYZED",
      "CHAPTER_RESOLVED",
      "SEARCH_ROUTE_SELECTED",
      "SEARCH_COMPLETED",
      "CANDIDATES_READY"
    ],
    "allowed_actions": [
      "set_chapter",
      "select_candidate",
      "reject_candidates",
      "show_candidates",
      "cancel"
    ],
    "next_stage": "SELECT_CANDIDATE",
    "chapter": "2静定结构",
    "candidate_count": 3,
    "candidate_generation": "1:1"
  },
  "current_unit": {
    "unit_id": "g1-u2",
    "page_index": 2,
    "display_label": "四-2",
    "status": "ACTIVE"
  },
  "units": [
    {
      "unit_id": "g1-u2",
      "page_index": 2,
      "display_label": "四-2",
      "status": "ACTIVE"
    }
  ],
  "consistency": {"status": "OK", "codes": []}
}
```

核心规则：

1. 父 `phase/status/task_revision` 永远只解释父状态，子字段永远只解释 A2 状态；phase namespace 固定为 `workflow` 和 `child_task`。
2. `active_child_task` 只表示当前权威的 0/1 个子题。当前存储没有历次子题的完整权威状态，V1 禁止伪造 `child_tasks[]` 历史。
3. `completed_steps` 只表示**当前权威字段仍能证明的里程碑**，不是完整事件历史。字段已被清理后，即使历史上发生过，也必须省略；不得从 response、trace 或日志补回。
4. `allowed_actions` 是真实授权动作与当前运行时可执行能力的交集。phase 候选集合不是无条件允许集合，执行时仍必须重新校验参数和当前状态。
5. `next_stage` 是当前视图，不是执行承诺；`SYSTEM_CONTINUE` 不承诺 HTTP 断开后继续，`RETRY` 也不引入阶段 5 的幂等保证。
6. `current_unit` 只在当前确实处理某个 A3 单元时存在。父完成、切题或回到选题后可能保留旧 `selected_unit_id`，不得因此继续输出 `current_unit`。
7. `units[]` 只包含父 `units` 中 `searchability == "searchable_candidate"` 的单元；不把不可检索或不确定单元伪装成 `AVAILABLE`。
8. 一致快照中最多一个 `ACTIVE` unit，且必须与 `current_unit` 是同一对象。A3 下行 A2 时，父、`current_unit` 和 child 视图使用同一个父 `selected_unit_id`；这只是弱绑定投影，不是 child 自身持久证据。

阶段 3.1 只冻结动作 ID、phase 候选集合和字段谓词的名称；它不读取 live state，也不执行权限判断。下文标为“3.2 验收”的条件必须由锁内权威构造器在生成快照前计算，`PhaseContract.action_candidates` 不能被当作无条件授权。

## 父 workflow phase、route 与 unit 矩阵

### Phase 归一化

| A3 phase | 统一 status | next_stage | 说明 |
| --- | --- | --- | --- |
| `IDLE` | `IDLE` | `UPLOAD_IMAGE` | 只用于不存在的 workflow 空投影 |
| `UNDERSTANDING_PAGE` | `RUNNING` | `SYSTEM_CONTINUE` | 路由判断或整页理解中的同步阶段 |
| `AUTO_GROUNDING_PAGE` | `RUNNING` | `SYSTEM_CONTINUE` | 自动框选中 |
| `AUTO_VALIDATING_CROPS` | `RUNNING` | `SYSTEM_CONTINUE` | 自动裁图并发校验中 |
| `WAIT_UNIT_SELECTION` | `WAITING_USER` | `SELECT_UNIT` | 等待选择剩余题目 |
| `CROP_REQUIRED` | `WAITING_USER` | `SUBMIT_CROP` | 当前题等待人工裁剪或重新提交 |
| `VERIFYING_CROP` | `RUNNING` | `SYSTEM_CONTINUE` | 裁图校验中 |
| `A2_ACTIVE` | `RUNNING` | `FOLLOW_CHILD_TASK` | 父不复制子 status；实际等待内容看 child |
| `COMPLETE` | `COMPLETED` | `DONE` | 生命周期结束，不等同于“检索成功” |
| `ERROR` | `FAILED` | `RETRY` | 仅在真实重试条件成立时输出重试动作 |
| `UNKNOWN` | `INCONSISTENT` | `RETRY` | 未知或不可安全解释的原始阶段哨兵 |

父为 `A2_ACTIVE`、子为 `WAIT_CANDIDATE_CHOICE` 时，合法组合是父 `RUNNING`、子 `WAITING_USER`。不得把父 status 改写成子 status。

### Route/phase/current-unit/child 合法矩阵

| 拓扑与 route | 合法父 phase | `current_unit` | `active_child_task` | 约束 |
| --- | --- | --- | --- | --- |
| 无父 workflow，`NONE` | `IDLE` | 无 | standalone A2 可有；A3 wrapper 不可有 | 空父投影固定 `exists=false`、`task_revision=0` |
| A3 wrapper，`PENDING` | `UNDERSTANDING_PAGE`、`ERROR` | 无 | 无 | 对应父 `entry_route == ""` |
| A3 wrapper，`A1` | `COMPLETE` | 无 | 无 | A1 停止仍是父生命周期完成，不表示搜题成功 |
| A3 wrapper，direct `A2` | `A2_ACTIVE` | 无 | 必须有 | child 不带 unit |
| A3 wrapper，`A3` | `UNDERSTANDING_PAGE`、`AUTO_GROUNDING_PAGE`、`AUTO_VALIDATING_CROPS`、`WAIT_UNIT_SELECTION`、`ERROR` | 无 | 无 | 原始残留 `selected_unit_id` 不投影为 current |
| A3 wrapper，`A3` | `CROP_REQUIRED`、`VERIFYING_CROP` | 必须恰有一个 | 无 | current unit 必须可检索且未关闭 |
| A3 wrapper，`A3` | `A2_ACTIVE` | 必须恰有一个 | 必须有 | child 视图 unit 来自父 selected unit |
| A3 wrapper，`A3` | `COMPLETE` | 无 | 无 | 所有导出 unit 必须是 `COMPLETED` 或 `CLOSED` |

不在矩阵中的组合返回 `WORKFLOW_ROUTE_PHASE_MISMATCH` 或 `WORKFLOW_ROUTE_UNIT_MISMATCH`。`COMPLETE` 仍存在 `AVAILABLE`、`PREPARED` 或 `ACTIVE` unit 时返回 `WORKFLOW_COMPLETE_UNIT_OPEN`。

## 父 completed_steps 的精确谓词

父里程碑词汇和固定输出顺序为：

```text
IMAGE_ACCEPTED
ROUTE_DECIDED
PAGE_UNDERSTOOD
UNIT_CATALOG_READY
UNIT_SELECTED
CHILD_TASK_STARTED
WORKFLOW_COMPLETED
```

`UNIT_CATALOG_READY` 表示题目单元目录已经从整页理解结果物化，不表示 `prepare_units()` 或自动裁图批次完成。

派生谓词：

```text
IMAGE_ACCEPTED
iff bool(parent.source_page_path)

ROUTE_DECIDED
iff parent.entry_route in {"A1", "A2", "A3"}

PAGE_UNDERSTOOD
iff parent.entry_route == "A3"
    and parent.page_understanding 是非空 mapping

UNIT_CATALOG_READY
iff PAGE_UNDERSTOOD

UNIT_SELECTED
iff parent.entry_route == "A3"
    and (
        bool(parent.selected_unit_id)
        or bool(parent.completed_unit_ids)
        or bool(parent.searched_unit_ids)
        or parent.crop_drafts 中存在属于当前 parent.units 的 key
    )

CHILD_TASK_STARTED
iff (
        parent.phase == "A2_ACTIVE"
        and 当前弱绑定 child 有效
    )
    or bool(parent.completed_unit_ids)
    or bool(parent.searched_unit_ids)

WORKFLOW_COMPLETED
iff parent.phase == "COMPLETE"
```

`page_understanding` 与 `units` 在当前 `_finish_page_understanding()` 中同一步赋值，因此即使物化结果为 `units=[]`，也可由 `PAGE_UNDERSTOOD` 证明 `UNIT_CATALOG_READY`。取消当前题可能清除 selected/child 且不留下历史字段；此时必须省略无法再证明的 `UNIT_SELECTED` 或 `CHILD_TASK_STARTED`。

按 phase 的最低可证明内容：

- `UNDERSTANDING_PAGE`：至少 `IMAGE_ACCEPTED`；若 route 已确定，再有 `ROUTE_DECIDED`；
- `AUTO_GROUNDING_PAGE|AUTO_VALIDATING_CROPS|WAIT_UNIT_SELECTION`：前四步；
- `CROP_REQUIRED|VERIFYING_CROP`：前四步加 `UNIT_SELECTED`；
- A3 route 的 `A2_ACTIVE`：再加 `CHILD_TASK_STARTED`；
- direct A2 的 `A2_ACTIVE`：只要求 `IMAGE_ACCEPTED|ROUTE_DECIDED|CHILD_TASK_STARTED`，不伪造 page/unit 步骤；
- `COMPLETE`：按字段累计，再加 `WORKFLOW_COMPLETED`；
- `ERROR`：只按字段谓词累计，不能根据 ERROR 猜测失败发生位置。
- `UNKNOWN`：不根据未知 phase 或不可读原始状态补全任何步骤；V1 上限为空，构造器必须使用稳定 `UNKNOWN` 哨兵并清空动作。

## 父 allowed_actions 的字段谓词（3.2 验收）

父 `allowed_actions` 先受 phase 候选集合限制，再按当前父状态和入口能力过滤。表中“存在”表示至少存在一个可立即执行的合法具体调用；上传事件和请求参数不是快照字段，必须由入口在构造时提供可信证据。

| 动作 | 最低字段/能力条件 |
| --- | --- |
| `upload_image` | 有可信的新题图上传事件；没有事件时不从当前状态臆造该动作 |
| `reset_session` | 当前会话暴露了显式清空入口；不得由异常或过期状态自动触发 |
| `retry_current_stage` | `phase == ERROR`，且仍有可读取的 `source_page_path` 与可用的页面重试路径 |
| `select_unit` | `route == A3`、`page_finished == false`，且存在 `searchable_candidate` 的未完成/未关闭剩余单元 |
| `prepare_units` | `route == A3`、`auto_crop_enabled == true`、phase 为 `WAIT_UNIT_SELECTION|CROP_REQUIRED`，且存在剩余单元 |
| `submit_crop` | `route == A3`、phase 为 `CROP_REQUIRED`，且 `selected_unit_id` 指向可检索的当前单元 |
| `cancel_current_unit` | `route == A3`、phase 为 `CROP_REQUIRED|A2_ACTIVE`，且当前选中单元存在并未完成/关闭 |
| `finish_page` | `route == A3`、`page_finished == false`，并且当前 phase 候选集合包含该动作 |

`continue_current` 是 A3 意图层的会话内话术动作，不进入 V1 公共 `TASK_ACTIONS`；`SYSTEM_CONTINUE` 只表示同步阶段仍在运行。`upload_image`、`reset_session` 的具体入口授权也不由快照状态自行扩展。

## 当前子题 phase、动作与 completed_steps

### Phase 归一化

| A2 phase | 统一 status | next_stage |
| --- | --- | --- |
| `IDLE` | `IDLE` | `UPLOAD_IMAGE` |
| `PROCESSING`、`READY_TO_ROUTE`、`READY_FOR_SEARCH` | `RUNNING` | `SYSTEM_CONTINUE` |
| `WAIT_CHAPTER` | `WAITING_USER` | `SET_CHAPTER` |
| `WAIT_QUESTION_CHOICE` | `WAITING_USER` | `SELECT_QUESTION` |
| `WAIT_CANDIDATE_CHOICE` | `WAITING_USER` | `SELECT_CANDIDATE` |
| `ANSWERED` | `COMPLETED` | `DONE` |
| `NO_MATCH` | `NO_MATCH` | `DONE` |
| `CANCELLED` | `CANCELLED` | `DONE` |
| `ERROR` | `FAILED` | `RETRY` |
| `UNKNOWN` | `INCONSISTENT` | `RETRY` |

`ANSWERED` 表示当前检索代次已经准备答案结果，仍可改章节、换候选或重发答案；它不是不可继续的永久终态。`CANCELLED` 通常只存在于锁内冻结响应，live store 随即清除，禁止从历史 response 或 trace 伪造为当前状态。

### A2 allowed_actions 使用真实动作 ID

子题 `allowed_actions` 直接使用 `ActionDecisionV2` 的真实业务 ID，不维护第二套公共别名：

- 使用 `cancel`，不使用 `cancel_child_task`；
- 使用 `retry_search`，不使用 `retry_child_task`；
- 包含真实的 `report_answer_mismatch`、`explain_failure`；
- 不包含 `continue_search`，因为当前权限矩阵从未允许该动作；
- 不包含 `search_image`，因为它创建或替换任务，属于 workflow/session 入口动作；
- `set_chapter` 在本字段中只表示 `chapter_target=current_question`；`next_image` 偏好不属于当前 child 动作。

`allowed_actions` 是权限矩阵允许与运行时当前可立即执行能力的交集。三个同步内部阶段即使纯权限矩阵可解析 `cancel`，当前运行时也没有阶段 5/6 的中途取消能力，因此公共集合为空。

以下集合假定 child 本身有效；所有 `+` 项只在字段谓词成立时加入：

| A2 phase | 对外 `allowed_actions` |
| --- | --- |
| `IDLE` | `[]`；不构造 active child |
| `PROCESSING` | `[]` |
| `READY_TO_ROUTE` | `[]` |
| `READY_FOR_SEARCH` | `[]` |
| `WAIT_CHAPTER` | `cancel`; + `set_chapter` iff `active_image_path`; + `global_search` iff `active_image_path && global_search_offered`; + `select_question` iff `question_count>0`; + `explain_failure` iff `last_error` |
| `WAIT_QUESTION_CHOICE` | `cancel`; + `select_question` iff `question_count>0`; + `explain_failure` iff `last_error` |
| `WAIT_CANDIDATE_CHOICE` | `cancel`; + `set_chapter` iff `active_image_path`; + `select_candidate|reject_candidates|show_candidates` iff `candidate_count>0`; + `select_question` iff `question_count>0`; + `explain_failure` iff `last_error` |
| `ANSWERED` | `cancel`; + `set_chapter` iff `active_image_path`; + `select_question` iff `question_count>0`; + `select_candidate|reject_candidates|show_candidates` iff `candidate_count>0`; + `resend_answer` iff `last_answer_paths`; + `report_answer_mismatch` iff `candidate_count>0 && last_answer_paths`; + `explain_failure` iff `last_error` |
| `NO_MATCH` | `cancel`; + `set_chapter` iff `active_image_path`; + `select_question` iff `question_count>0`; + `explain_failure` iff `last_error` |
| `ERROR` | `cancel`; + `set_chapter` iff `active_image_path`; + `retry_search` iff `active_image_path && retryable_error`; + `select_question` iff `question_count>0`; + `select_candidate` iff `candidate_count>0`; + `explain_failure` iff `last_error` |
| `CANCELLED` | `[]`；只用于冻结响应 |

索引、章节、候选 generation 等参数在执行时必须再次走真实权限矩阵；动作 ID 出现在快照中，只表示至少存在一个合法具体调用。

### 子题 completed_steps 的精确谓词

子题里程碑词汇和固定输出顺序为：

```text
QUESTION_ACCEPTED
QUESTION_ANALYZED
CHAPTER_RESOLVED
SEARCH_ROUTE_SELECTED
SEARCH_COMPLETED
CANDIDATES_READY
ANSWER_PREPARED
```

V1 不使用 `ANSWER_DELIVERED`：`last_answer_paths` 只能证明答案媒体已准备，不能证明网络交付成功。V1 也不使用 `TASK_FINISHED`：`ANSWERED` 仍允许继续操作，现有字段没有统一、不可逆的 child finished 事实。

派生谓词：

```text
QUESTION_ACCEPTED
iff child.task_revision > 0
    and bool(child.current_search_id)
    and bool(child.current_image_path)

QUESTION_ANALYZED
iff QUESTION_ACCEPTED
    and 下列任一分析或下游证据存在：
        current_question_image_path
        current_loads
        questions
        selected_question
        chapter_scope_status
        current_chapter
        candidates
        last_answer_paths

CHAPTER_RESOLVED
iff bool(child.current_chapter)

SEARCH_ROUTE_SELECTED
iff child.current_route in {"main", "symbolic"}

SEARCH_COMPLETED
iff child.current_route in {"main", "symbolic"}
    and child.candidate_revision > 0

这里只承认题库搜索路由 `main|symbolic`。图片分流的 `A1|A3` 是入口决定，
不是题库搜索路由；即使它们令 `candidate_revision` 增长或产生空候选，也不得
产生 `SEARCH_ROUTE_SELECTED` 或 `SEARCH_COMPLETED`。

CANDIDATES_READY
iff bool(child.candidates)
    and child.candidate_revision > 0
    and child.candidate_generation
        == f"{child.task_revision}:{child.candidate_revision}"

ANSWER_PREPARED
iff child.phase == "ANSWERED"
    and bool(child.last_answer_paths)
    and child.selected_rank 是当前 candidates 的合法排名
```

按 phase 的最低可证明内容：

- `PROCESSING`：`QUESTION_ACCEPTED`；
- `WAIT_CHAPTER|WAIT_QUESTION_CHOICE`：再加 `QUESTION_ANALYZED`；
- `READY_TO_ROUTE`：再加 `CHAPTER_RESOLVED`；
- `READY_FOR_SEARCH`：再加 `SEARCH_ROUTE_SELECTED`；
- `WAIT_CANDIDATE_CHOICE`：再加 `SEARCH_COMPLETED|CANDIDATES_READY`；
- `ANSWERED`：再加 `ANSWER_PREPARED`；
- `NO_MATCH`：只按字段累计；只有 `current_route && candidate_revision>0` 才证明题库搜索已完成，外荷载门提前结束不得伪造 `SEARCH_COMPLETED`；
- `ERROR|CANCELLED`：只按字段谓词累计，不根据 phase 猜测此前走到哪一步；
- `UNKNOWN`：不输出任何 completed step；未知原始 phase 只能映射为稳定 `UNKNOWN` 并 fail-closed；
- `IDLE`：无步骤，不构造 active child。

对外 child view 不单独暴露 `candidate_revision`：当 `candidate_count == 0` 时
`candidate_generation` 必须为空；当候选非空时，generation 必须是
`<正 task_revision>:<正 candidate_revision>`，且第一段必须等于当前
`task_revision`。3.1 同时冻结纯校验入口
`validate_candidate_generation(task_revision, candidate_count, value, candidate_revision=None)`：
省略最后一个参数时校验格式和 task 前缀；传入权威 `candidate_revision` 时，非空候选必须核对
两段的精确值和正代次，空候选只要求 generation 为空（权威计数可因空结果而递增）。3.2 构造器必须在构造 view 前调用带权威代次的形式；不匹配时返回
`CHILD_CANDIDATE_GENERATION_MISMATCH`，不能静默省略里程碑后继续输出可执行动作。

## Unit 互斥状态

先限定导出集合：`units[]` 只遍历 `parent.units` 中 `searchability == "searchable_candidate"` 的单元。

对外每个 unit 只允许一个状态，优先级固定为：

```text
COMPLETED > CLOSED > ACTIVE > PREPARED > AVAILABLE
```

在应用优先级前，必须先检查：

```text
set(completed_unit_ids) ∩ set(searched_unit_ids) == ∅
```

交集非空是 `UNIT_STATE_OVERLAP`，必须整体 fail-closed，不能用 `COMPLETED` 优先级吞掉冲突。

状态谓词：

- `COMPLETED`：`unit_id in completed_unit_ids`；
- `CLOSED`：`unit_id in searched_unit_ids`，或 `parent.page_finished` 且该 unit 未完成；旧 `searched_unit_ids` 实际包含被切换或停止的题，禁止展示成“已检索”；
- `ACTIVE`：父 route 为 `A3`、phase 为 `CROP_REQUIRED|VERIFYING_CROP|A2_ACTIVE`、`selected_unit_id == unit_id`，且该 unit 未完成、未关闭；
- `PREPARED`：workflow 不在 `COMPLETE|ERROR`，该 unit 未完成、未关闭、非 active，并且 `auto_crop_enabled is True`、`auto_crops[unit_id].validation_status == "auto_ready"`、对应 path 为非空字符串且在受控 session crop 目录中当前确实是文件。path 只用于构造器的存在性检查，不进入公共快照；
- `AVAILABLE`：其余仍可检索的 unit。

旧的或残留的 auto-crop ready 记录不得覆盖更高优先级状态：unit 已完成、关闭或 active 时，`PREPARED` 条件被忽略。父 `WAIT_UNIT_SELECTION` 或 `COMPLETE` 中残留的 `selected_unit_id` 也不得产生 `ACTIVE`。

## 同 session 弱绑定与阶段 5 边界

V1 只定义同会话当前态的**弱绑定**，不声明持久父子外键。现有 `AgentState` 不保存 `workflow_search_id` 或 `unit_id`。

A3 wrapper 只有在同一 session lock 内同时满足以下条件，才可投影 `active_child_task`：

1. 父 `phase == A2_ACTIVE`；
2. 父、子持久状态的 `session_id` 都等于本次查询的规范化 session ID；
3. child `current_search_id` 非空，且 child 是可读的 live state；
4. direct A2 route 不要求 unit；
5. A3 route 要求父 `selected_unit_id` 指向一个未关闭的 `searchable_candidate`。

A3 route 下 child 视图中的 `unit_id` **来自父 `selected_unit_id`**，provenance 是 parent，不是 child 自身持久证据。因此 V1 无法验证 `CHILD_UNIT_MISMATCH`，该 code 从 V1 删除；不得通过把同一个父 unit ID 同时填入 child/current-unit 来声称完成了强绑定校验。

父 `workflow_id` 与 child `task_id` 必须不同；相同返回 `PARENT_CHILD_ID_COLLISION`。强父子 workflow ID/unit ID 外键、历次 child 历史和可恢复生命周期属于阶段 5。

父已经回到选题或完成阶段后，A2 store 中残留的旧记录不是 active child，直接忽略，不从历史记录制造冲突。

父记录缺失但 child 存在时：

- 只有运行拓扑明确为 standalone A2，才可输出根级 `active_child_task`；
- 在 A3 wrapper 拓扑中必须返回 `ORPHAN_CHILD_TASK`，不得因为父 `load()` 返回 `None` 就把孤儿 child 冒充 standalone A2。

## 3.2.1 纯构造器边界

`build_task_state_snapshot_v1(read_set, evidence)` 已实现本文件的纯投影。它只接受调用方已经冻结的父子状态与非状态证据，不读 store、不获取锁、不访问文件系统、不写日志，也不修改输入状态。

`TaskStateReadSet` 显式携带：

- 已规范化的 `session_id` 和可信 topology；
- 父、子各自独立的 `OK|MISSING|UNREADABLE|UNKNOWN_PHASE` 读取结果，父额外允许 `DUPLICATE_UNIT_ID`；
- `OK` 必须携带对应状态，其他结果必须不携带状态，因此 missing 与 unreadable 不会被构造器混同；
- child observation 的 `LIVE|RESPONSE_FROZEN` provenance：LIVE `CANCELLED` 是已清理残留，不投影为 active child；锁内冻结响应才可投影 `CANCELLED`。

`TaskStateBuildEvidence` 只携带由调用方预先验证的事实：可信新图事件、reset 入口能力、与父原图精确匹配的可读路径证据、父重试能力、与 `(child_id, task_revision)` 精确匹配的子任务重试证据，以及 `(unit_id, crop_path)` 精确匹配的受控真实文件证据。构造器只比较精确值；路径可读、文件存在和受控目录包含性必须由 3.2.2 调用方证明。

任何一致性 code 出现时，纯构造器统一清空可见动作与 unit 投影，将仍可安全表示的对象标为 `INCONSISTENT/RETRY`；若保留 child 会再次违反父子结构约束，则直接省略 child，只保留稳定 code。

## 锁内 read-set、读取失败与 task_revision

### 单次一致 read-set

统一快照必须在对应 runtime 的完整 per-session 锁集合内，由一次父子 read-set 构造。A3 wrapper 与 standalone A2 的锁并不是同一把：

- A3 wrapper 固定沿用现有锁序：先取得 A3 session lock，再取得同 session 的 A2 session lock，分别各 `load()` 一次父/子状态，按 A3→A2 顺序获取并逆序释放；不得反向先持有 A2 锁再请求 A3 锁；
- 若调用方已经在 A2 非重入锁内（例如 A2 响应路径），必须把锁内冻结的 `AgentState` 传给纯构造器，禁止构造器再次获取 A2 锁；
- standalone A2 在 A2 session lock 内读取 child；
- 业务响应使用锁内已经冻结的状态对象，解锁后不得重新读取 live store 覆盖；
- `/api/session` 也必须先取锁，再一次性构造；
- 不得用父子 `task_revision` 相等、大小或变化来猜测 read-set 是否原子。

### `None` 与 unreadable 严格分离

`store.load()` 返回 `None` 只表示记录不存在或已过期；JSON、schema、未知 phase 或状态校验异常属于 unreadable：

- 父读取异常返回 `WORKFLOW_STATE_UNREADABLE`；
- 子读取异常返回 `CHILD_STATE_UNREADABLE`；
- 不得把读取异常降级成 `IDLE`、missing 或 standalone；
- 异常正文只进入内部诊断，不进入公共快照。

若 store 层能以稳定类型识别原始未知 phase 或重复 unit，可分别返回 `UNKNOWN_WORKFLOW_PHASE|UNKNOWN_CHILD_PHASE` 或 `DUPLICATE_UNIT_ID`；若当前 `load()` 已在返回对象前统一抛出而无法稳定分类，则使用对应 `*_STATE_UNREADABLE`，禁止解析异常文字猜 code。

### `task_revision` 不是快照版本

JSON 字段固定名为 `task_revision`，不得缩写成含义模糊的 `revision`：

- 父 `task_revision` 在新父题图开始时递增；
- 子 `task_revision` 在每次 `start_search()` 时递增，包括同一 `search_id` 的重试；
- phase 变化、选择候选、重发答案和多数文本动作不会递增它；
- 父、子 revision 位于独立命名空间，禁止比较、拼接或用其证明父子绑定；
- 它只可拒绝上一代题图/任务动作；候选集合的新旧继续使用 `candidate_generation`；
- V1 没有覆盖全部状态变化的 `snapshot_revision`，通用任务版本和乐观并发控制属于阶段 5。

一致的已存在父 workflow 与当前 child 的 `task_revision` 必须为正数；`0` 只用于不存在 workflow 的空投影或带相应冲突码的清洗占位。父、子 revision 即使数值相同也仍属于不同命名空间，不能互相证明。

## 一致性 codes 与 fail-closed

V1 consistency code 精确限定为以下 17 个：

| code | 触发条件 |
| --- | --- |
| `WORKFLOW_ID_MISSING` | 父记录存在但权威 `workflow_search_id` 为空 |
| `CHILD_TASK_ID_MISSING` | 需要投影当前 child，但 `current_search_id` 为空 |
| `ACTIVE_CHILD_TASK_MISSING` | 合法矩阵要求 active child，但 child 记录不存在 |
| `ACTIVE_UNIT_MISSING` | A3 phase 要求 current unit，但 selected unit 不存在或不在导出单元中 |
| `ACTIVE_UNIT_CLOSED` | 需要 active 的 selected unit 已完成、已关闭或 page 已结束 |
| `UNIT_STATE_OVERLAP` | `completed_unit_ids` 与 `searched_unit_ids` 有交集 |
| `DUPLICATE_UNIT_ID` | 权威 unit 集合存在重复 ID，且读取层可稳定分类 |
| `UNKNOWN_WORKFLOW_PHASE` | 权威父 phase 不在已审查枚举，且读取层可稳定分类 |
| `UNKNOWN_CHILD_PHASE` | 权威子 phase 不在已审查枚举，且读取层可稳定分类 |
| `PARENT_CHILD_ID_COLLISION` | 父 `workflow_id` 与 child `task_id` 相同 |
| `ORPHAN_CHILD_TASK` | A3 wrapper 中父缺失但 child 存在 |
| `WORKFLOW_STATE_UNREADABLE` | 父 JSON/schema/状态无法安全装载或无法细分读取错误 |
| `CHILD_STATE_UNREADABLE` | 子 JSON/schema/状态无法安全装载或无法细分读取错误 |
| `WORKFLOW_ROUTE_PHASE_MISMATCH` | route 与父 phase 不符合合法矩阵 |
| `WORKFLOW_ROUTE_UNIT_MISMATCH` | route/phase 与 current-unit 是否应存在不符合合法矩阵 |
| `WORKFLOW_COMPLETE_UNIT_OPEN` | `COMPLETE` 仍有 `AVAILABLE|PREPARED|ACTIVE` unit |
| `CHILD_CANDIDATE_GENERATION_MISMATCH` | 候选非空但 generation 与子 task/candidate revision 不一致 |

`CHILD_UNIT_MISMATCH` 明确不属于 V1：child 没有持久 unit 字段，弱绑定下不可验证。

任一 consistency code 出现时必须：

1. `consistency.status = INCONSISTENT`；
2. 清空 workflow 和 child 的全部 `allowed_actions`；
3. 有可读父时令父 `status=INCONSISTENT`；无父的 standalone/orphan 情况令 child `status=INCONSISTENT`；
4. `next_stage=RETRY`，但不因此凭空增加重试动作；
5. 未知或不可读 phase 对外使用稳定 `UNKNOWN`，不得透传任意原始值；
6. 不从 response、trace、日志或另一个入口拼装替代状态来掩盖冲突。

## 响应时序与兼容边界

- 业务响应继续优先使用持有 session lock 时冻结的 response-time state，不能在响应末尾重新读取 live 状态替换；
- 媒体交付失败使任务重新打开时，应使用重新打开后的状态；历史 response 投影仍只保留当次交付事实；
- 阶段 3.2/3.3 新增统一字段时，现有顶层 `phase/search_id/task_revision/chapter/candidate_count/a3` 暂留兼容，不能原地改义；
- `a3: null` 的旧前端清空语义必须保留；
- stream 继续使用 `progress* -> result/error`；统一状态放进已有 result data，不增加尾随状态事件；
- V1 不含 API key、邀请码、身份哈希、用户/模型原文、本地路径、URL、异常正文或任意扩展字典。

## 后续批次

1. **3.2.1 纯构造器（DONE）**：已完成从冻结 read-set 和可信入口证据到 V1 快照的无 I/O 投影，实现与定向测试见 `tiku_agent/task_state_builder.py` 和 `tests/test_task_state_builder.py`。
2. **3.2.2 锁内权威读取（NEXT）**：在 A3→A2 锁序内一次读取父子状态，分类缺失/不可读、证明受控文件与入口能力，再调用纯构造器；不得重复加锁或解锁后重读 live state。
3. **3.3 出口一致性（尚未实现）**：接入 `/api/session`、JSON success、stream result 和受控错误出口，保持 response-time 冻结与旧字段兼容。
4. **3.4 前端消费（尚未实现）**：浏览器消费服务端 `allowed_actions/next_stage`，逐步删除对父子 phase 和 unit flag 的动作拼装。
5. **3.5 启用门（尚未实现）**：定向/全量回归、8896 契约烟测、只读 live 对照后再精确启用 8790；不触碰 8788/8794/8795。
