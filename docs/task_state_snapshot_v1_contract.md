# 统一任务状态快照 V1 契约（阶段 3.1 / 3.2 / 3.3 / 3.4）

## 结论与范围

阶段 3.1 完成父 workflow、当前子题任务和题目单元的权威状态契约定稿。本文件冻结 V1 的字段、词汇、字段来源、派生谓词、拓扑边界、一致性错误和冲突策略，是后续实现的权威规范。阶段 3.2.1 已实现从“已冻结 read-set + 可信入口证据”到 `TaskStateSnapshotV1` 的纯构造器，落在 `tiku_agent/task_state_builder.py`；阶段 3.2.2 已实现 `tiku_agent/task_state_runtime.py` 的锁内读取与证据包装，并由 `A3MvpRuntime.task_state_snapshot_v1()`、`AgentSessionRuntime.task_state_snapshot_v1()` 和 `AgentSessionRuntime.task_state_snapshot_v1_from_frozen_state()` 提供内部运行时入口；阶段 3.2.3 已用实际构造结果矩阵和组合读取异常完成边界验收。阶段 3.3.1 冻结 exact typed 公共映射，3.3.2～3.3.5 分别接入 `/api/session`、HTTP 200 业务 JSON、受控非 stream HTTP 4xx/5xx 和五条任务 stream 终态；3.3.6 已完成跨出口 parity、失败后处理、兼容入口和全仓回归验收。

**阶段 3.3 出口一致性及 3.4.1～3.4.2 前端 model/信封接线已在代码中完成，但尚未部署启用到 8790。** 当前受控 HTTP 出口在根级返回 `task_state`，五条任务 stream 的 success 在 `event.data.task_state`、非准入 error 在 `event.task_state` 返回 exact typed V1；浏览器已按这些权威层级原子消费，busy/queue、progress 和非任务响应保持 no-update。旧按钮授权尚未迁移，3.5 启用门仍未完成，因此主阶段 3 仍为 `IN_PROGRESS`，不能声称已经上线。既有内部 `session_snapshot()` 仍是兼容投影，不等同于统一快照。

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

`TaskStateBuildEvidence` 只携带由调用方预先验证的事实：可信新图事件、reset 入口能力、与父原图精确匹配的可读路径证据、父重试能力、与 `(child_id, task_revision)` 精确匹配的子任务重试证据，以及 `(unit_id, crop_path)` 精确匹配的受控真实文件证据。构造器只比较精确值；3.2.2 runtime wrapper 已在锁内验证路径可读、文件存在、受控目录包含性和精确身份绑定，入口能力仍只能由实际调用入口显式提供。

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

3.2.2 的实现映射如下：A3 wrapper 固定在 A3→A2 双锁内让父、子 store 各 `load()` 一次，并在完成证据验证和 V1 投影后逆序释放；standalone A2 只获取 A2 锁并单读 child；已持有 A2 锁的调用方可使用 frozen-state 入口，该入口不获取锁、不读取 store。读取异常正文不会进入快照；稳定未知 phase 和重复 unit 独立分类，文件探测失败只撤销对应动作证据。3.2.2 完成时这里只提供可调用的内部 runtime 能力；3.3.2 由 `/api/session`、3.3.3 由 HTTP 200 业务 JSON、3.3.4 由受控 HTTP error、3.3.5 由 stream result/error 复用组合捕获和响应时冻结入口。

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

## 3.2.3 异常与矩阵验收

阶段 3.2.3 不增加新的生产行为，只补齐对既有 3.2 实现的运行时证明：

- 实际构造器覆盖空记录 `NONE/IDLE`，穷举可持久化的 `PENDING/A1/A2/A3` 合法父 route/phase，并验证这些 route 的全部非法 phase 组合均以 `WORKFLOW_ROUTE_PHASE_MISMATCH` fail-closed；
- 九个 live child phase 分别在 standalone A2、direct A2 和 A3 active 三种载体上投影，另覆盖 `IDLE`、live/frozen `CANCELLED`、稳定未知 phase 与非 active 残留 child；
- A3 active child 不可读及父子同时不可读时，仍保持 A3→A2 锁序、每个 store 单次读取、逆序释放、稳定错误码、全动作清空和异常正文脱敏；
- 同一 child ID 的旧 `task_revision` 不得获得 `retry_search`。

项目 Python 3.12 下 51 项 task-state 定向测试及全仓 1035 项回归通过。因此阶段 3.2 整体 DONE；截至 3.2 验收时，公共 HTTP/stream、前端消费和 8790 启用仍分别属于 3.3～3.5。当前 3.3.2～3.3.5 已接入 session、业务 JSON、受控 HTTP error 与 stream 终态，仍未部署。

## 响应时序与兼容边界

- 业务响应继续优先使用持有 session lock 时冻结的 response-time state，不能在响应末尾重新读取 live 状态替换；
- 媒体交付失败使任务重新打开时，应使用重新打开后的状态；历史 response 投影仍只保留当次交付事实；
- 阶段 3.2/3.3 新增统一字段时，现有顶层 `phase/search_id/task_revision/chapter/candidate_count/a3` 暂留兼容，不能原地改义；
- `a3: null` 的旧前端清空语义必须保留；
- stream 继续使用 `progress* -> result/error`；统一状态放进已有 result data，不增加尾随状态事件；
- V1 不含 API key、邀请码、身份哈希、用户/模型原文、本地路径、URL、异常正文或任意扩展字典。

## 3.3.1 公共映射契约（DONE；3.3.2～3.3.5 已接入出口）

3.3.1 只冻结把已由 runtime 构造的 `TaskStateSnapshotV1` 放入公共载荷的
纯映射边界，实现在 `tiku_agent/task_state_public.py`，不获取锁、不读取
store、不调用 `session_snapshot()`，也不改变任何 HTTP、stream 或前端行为。

### 字段位置与信封

- 后续公共载荷统一使用 `task_state` 名称，其值严格等于
  `TaskStateSnapshotV1.to_dict()` 的 V1 结构，但不改义旧 `session` 字段。
- JSON success 与 `/api/session` 的精确位置是 `payload.task_state`；
  stream success 的精确位置是 `event.data.task_state`；stream error 的精确
  位置是 `event.task_state`。
- HTTP error 只对具有会话/任务上下文的受控 Agent API 使用
  `payload.task_state`；静态资源、登录、独立媒体等无任务上下文的错误
  不得为了补字段而读 runtime。
- stream 仍只允许 `progress* -> result/error`；状态不能通过尾随事件补发。
- 3.3.1 不负责决定错误路径何时取得冻结快照；缺失/过期状态必须由调用方
  先构造成 `empty_task_state_snapshot()`，不可用旧混合快照或客户端字段代替。

### 纯映射与安全约束

- 映射函数只接受精确类型 `TaskStateSnapshotV1`，拒绝 subclass、任意
  `Mapping`、客户端传入的字典和未验证的旧 `session_snapshot()`；
  它直接调用基类的规范 serializer，不信任实例覆写，也不修改输入。
- 输出根及 workflow、child、unit、consistency 子对象使用精确白名单键，
  经 `allow_nan=False` 的 JSON round-trip 后再返回，确保没有非 JSON 值、
  NaN 或可变别名。
- `TaskStateSnapshotV1` 自身保证可发布：公共 ID、chapter 和 display label
  共用 `is_public_task_state_text()` 约束，拒绝 URL/本地路径、凭据与身份
  标记、异常样文及 prompt/debug 文本。纯 builder 使用同一判定，存储中
  的不安全 workflow/child 文本分别收口为 `WORKFLOW_STATE_UNREADABLE`
  / `CHILD_STATE_UNREADABLE`，不会先构造一个合法 typed 快照再在映射层失败。
- 映射层仍使用同一判定做不可达防线；若触发，说明服务端对象已被
  违反不变式地篡改，不得偷换成表示“会话不存在”的 empty 快照。
- `task_state` 始终由服务端快照覆盖，不能信任载荷中已有的同名字段。

本小步的定向测试位于 `tests/test_task_state_public.py`，覆盖空投影、完整
child/current-unit schema、JSON/stream result/error 同值、输入不变、subclass/
任意 mapping 拒绝和敏感值阻断；构造器测试另验证不安全存储文本
fail-closed。3.3.2 已接入 `/api/session` 的锁内快照，3.3.3 已接入
HTTP 200 业务 JSON，3.3.4 已接入受控非 stream HTTP error，3.3.5 已接入
stream result/error；后续只处理跨出口验收。

## 3.3.2 `/api/session` 接入（DONE；尚未部署）

`GET /api/session` 的成功载荷现在固定为三个根级 sibling：
`uploaded_image`、兼容字段 `session` 和权威 `task_state`。新增字段严格经过
3.3.1 的 `with_public_task_state()` 映射；旧 `session` 的键、含义和清洗逻辑
不变，A1/direct A2/standalone A2 下用于清除旧前端状态的 `a3: null` 语义
继续保留，恢复题图 URL、Cookie、缓存头和媒体会话隔离也不变。

为避免在同一 HTTP 响应中拼接不同时刻的数据，两个 runtime 新增
`session_response_snapshot_v1()` 组合捕获：

- standalone A2 先完成过期清理，再在 A2 session lock 内让 child store
  `load()` 一次，从同一冻结 `AgentState` 投影题图、旧 session 与 V1；
- A3 wrapper 先共同清理父子过期状态，再严格按 A3→A2 获取双锁，parent/
  child store 各 `load()` 一次，并在解锁前完成三部分投影；
- endpoint 不再分别调用 `current_image_path()`、`session_snapshot()` 和
  `task_state_snapshot_v1()`，也不提供会退回多次 live 读取的兼容 fallback；
- GET 没有可信新图事件，因此 `trusted_image_event=false`；同一 Web 入口确有
  `/api/reset`，因此显式设置 `reset_session_available=true`，最终动作仍受
  phase 候选和字段谓词过滤。

missing/expired 只输出标准 empty V1；parent missing + child live 仍按
`ORPHAN_CHILD_TASK` fail-closed。不可读状态不伪装成 200/IDLE：组合捕获以
`SessionResponseSnapshotError` 保留本次锁内 typed V1 和安全 legacy 占位，
通用 500 处理不会再次读取 store。3.3.2 完成时错误载荷尚未公开该字段，现已由
3.3.4 接入。邀请码 401 在进入 endpoint 前返回，不为补状态读取 runtime。

验收覆盖 standalone A2、A1/direct A2/A3 active、missing/orphan、不可读单读
失败、A3→A2 锁序、父子各单读、过期状态及 artifacts 清理、旧 session/a3
兼容、题图恢复与隔离、Cookie 属性、401 零读取和 8790/8890/8794 基线。
项目 Python 3.12 下 64 项 task-state 定向测试及全仓 1053 项回归通过。

## 3.3.3 HTTP 200 业务 JSON 接入（DONE；尚未部署）

本小步只扩展受控任务型 HTTP 200 JSON，不改变协议状态本身：

- `POST /api/message` 的正常回复、stale action/candidate 回复，以及
  `NEEDS_INPUT`、`NO_MATCH`、`PARTIAL`、协议级业务 `ERROR` 等仍以 HTTP 200
  返回的 `AgentResponse`；
- `POST /api/image` 的 HTTP 200 回复；
- A3 启用时 `POST /api/a3/select` 的 HTTP 200 回复；
- `POST /api/reset` 清理完成后返回 canonical empty V1；兼容 `search_id` 仍
  表示被清理的搜索，但不能把清理前的 live state 作为 `task_state`。

前三类载荷均保留现有根字段，并新增根级 `task_state`。能力证据固定如下：

| 出口 | `trusted_image_event` | `reset_session_available` | 状态来源 |
| --- | --- | --- | --- |
| `/api/message`（含 stale） | `false` | `true` | 响应生成时冻结的 read-set |
| `/api/image` | `true` | `true` | 同一次受信上传处理后冻结的 read-set |
| `/api/a3/select` | `false` | `true` | 响应生成时冻结的 read-set |
| `/api/reset` | 不适用 | 不适用 | `empty_task_state_snapshot()` |

响应一致性不能靠出口末尾补读：standalone A2 在既有 A2 session lock 内完成
业务操作后单读 child，并同时冻结 legacy、typed V1 和题图；A3 wrapper 在既有
A3 lock 内再按 A3→A2 顺序获取 child lock，让 parent/child store 各单读一次，
再从同一 read-set 派生 legacy、typed V1、裁图和 overlay。stale action/candidate
在拒绝时使用 `response_frozen=true` 的组合捕获；`_agent_payload()` 在 JSON
模式下必须收到冻结的 legacy/projection 和 exact typed V1，缺任一项即
fail-closed，并在任何媒体处理前完成校验，禁止回退到 `session_snapshot()`
事后读取 live runtime。standalone A2 的活跃任务取消在清库前冻结
`CANCELLED`；请求前没有 active child（missing、`IDLE` 或 live 残留
`CANCELLED`）时返回 canonical empty，不能把临时 revision 0 状态伪装成任务。

候选或答案媒体落盘失败是唯一允许替换首次冻结状态的后处理：A3 在同一
A3→A2 锁序内按预期 `unit_id`、`task_revision` 和 `candidate_generation` 校验，
成功重开任务后返回新的组合快照，JSON 同时替换为重开后的 legacy 与 typed
V1；替换前要求 exact `SessionResponseSnapshotV1`、非空 exact legacy dict 和
exact typed V1，组合不完整即 fail-closed。若 guard 已过期则保留原
response-time V1，不读取或伪造更新后的状态。
字段在 Response Store finalization 前进入最终 JSON 草稿，既有反馈绑定、终态
记录和公共输出清洗顺序不变。

3.3.3 本小步当时明确不把 `task_state` 加到 HTTP 4xx/5xx、stream result/error、
登录、反馈、静态/媒体资源或前端，也不为补字段额外读取 runtime。当前 HTTP error
已由 3.3.4 接管，stream 已由 3.3.5 接管；跨出口 parity 属于 3.3.6，前端和启用门
分别属于 3.4/3.5。3.3.3 本身没有部署、重启 8790，也没有触碰
8788/8794/8795。

验收覆盖普通 JSON、图片、A3 选择、stale、reset、HTTP 200 业务错误、媒体
失败重开、冻结媒体路径、取消、8890 shadow 能力透传、初始或重开组合缺失
时 fail-closed；完成 3.3.3 时曾明确断言 HTTP error 和所有 stream 终态仍无
`task_state`，这些断言现已分别由 3.3.4 和 3.3.5 更新。
项目 Python 3.12 下 65 项 task-state 定向测试、193 项相关出口测试及全仓
1072 项回归通过。

## 3.3.4 受控 HTTP error 接入（DONE；尚未部署）

本小步只覆盖具有已建立会话上下文的非 stream 受控 HTTP 4xx/5xx，精确路径为
`/api/session`、`/api/message`、`/api/image`、`/api/a3/select` 和
`/api/reset`。这些错误载荷在根级返回 exact typed V1 `task_state`；既有
`status/code/layer/detail/request_id/search_id`、`Retry-After`、
`Cache-Control`、`X-Request-ID`、Cookie 和 Response Store 行为保持兼容。

状态遵循“异常发生点冻结携带，HTTP 边界不事后重读”：runtime 已携带 legacy/V1
组合时直接复用；输入校验或普通异常尚无快照时，只进行一次
`response_frozen=true` 的组合捕获。standalone A2 保持单锁单读；A3 保持
A3→A2 锁序并让父子 store 各读取一次。`/api/reset` 失败使用清理尝试后的状态，
不回退到清理前快照，并保留原会话 Cookie。媒体重开后的映射错误同样携带
post-reopen 的 legacy/typed 组合，不混用重开前状态。

missing/expired 输出 canonical empty V1；状态读取、过期清理或公共映射不可用时，
不得伪装成 empty 或静默省略字段，而是返回脱敏的 `INCONSISTENT` typed V1。
A2 不可读使用 `CHILD_STATE_UNREADABLE`；A3 父子均不可用时使用对应 workflow/
child unreadable codes；父已清除但 child 清理失败时，单次 post-attempt 组合捕获
输出 `ORPHAN_CHILD_TASK`。store purge/readability 本身失败时不立即重读失败
store，而是零 I/O 输出 unreadable sentinel。异常正文、路径和内部对象均不进入
公共载荷。

无既有会话的输入拒绝不创建 session，也不添加 `task_state`。队列准入拒绝发生在
任何 session read-set 之前，是显式例外：即使异常对象意外携带 typed 值，也会在
HTTP 边界强制丢弃，保持零 store 读取且不制造状态。3.3.4 本身未覆盖 stream，
现由 3.3.5 精确覆盖五条任务 stream；登录、反馈、独立 upload/media、静态资源及
其他非任务路径继续不接入，也不为补字段读取 runtime。

验收覆盖精确五路径矩阵、400 输入拒绝、quota/protocol/unexpected 500/503、
A3 错误、reset 部分清理失败、missing/expired、状态不可读、session 与业务执行期
purge 失败、映射失败、媒体重开、无 Cookie、队列拒绝、排除路径、单次冻结读取及
A3→A2 锁序。项目 Python 3.12 下 65 项 task-state 定向测试、206 项相关
HTTP/runtime 出口测试及全仓 1116 项回归通过。

## 3.3.5 stream result/error 接入（DONE；尚未部署）

本小步精确覆盖五条已有任务 stream：`/api/message/stream`、
`/api/image/stream`、`/api/a3/select/stream`、`/api/a3/prepare/stream` 和
`/api/a3/crop/stream`。success 在已有 `result` 的 `event.data.task_state`
返回 exact typed V1，error 在 `event.task_state` 返回；事件顺序仍为
`progress* -> result/error`，progress 不带 `task_state`，也不增加尾随状态事件。
既有 legacy 字段、SSE 信封、Response Store、反馈绑定和唯一终态语义保持兼容。

成功终态必须使用业务响应点冻结的 legacy/V1，不得在生成 stream 终态时再读取
live state。`prepare_units()` 和 `handle_crop()` 也接受入口 capabilities，使 A3
prepare/crop 与 message/image/select 使用同一冻结契约。typed 图片出口必须携带
冻结的媒体存在性标志；缺失时 fail-closed，不能回退调用 live
`current_image_path()`。Response Store 或结果序列化在业务响应形成后失败时，
继续复用已经冻结的状态，不以失败时刻的 live 状态覆盖。

error 优先复用异常携带的 exact legacy/V1 pair；异常没有可信 read-set 时才允许
一次 `response_frozen=true` 的组合捕获。若异常仅带 legacy，或已尝试读取但组合
不完整，则零读取降级为按可信 runtime 拓扑构造的脱敏 `INCONSISTENT`，不得把
A3 wrapper 误判为 standalone A2，也不得从异常正文或 live store 拼状态。公共映射
失败同样按 runtime 拓扑收口为 workflow/child unreadable。所有五入口的
`AgentRuntimeBusyError`，以及 `QUEUE_FULL` / `QUEUE_TIMEOUT`，仍在 read-set 前
结束：零 store 读取且不返回 `task_state`。

媒体交付导致任务重开时，success 使用 post-reopen 的 exact pair；guard stale
保留原 response-time pair。重开抛错或返回非法组合时，使用可信 runtime 拓扑的
零读取 sentinel；不得混回重开前快照，也不得为补状态重新读取 store。该约束覆盖
候选、答案和上传图片的 stream 终态。

验收覆盖五入口 success/stale/error/busy、exact pair、legacy-only 与不完整 read-set、
映射/序列化/Response Store 失败、媒体重开 success/stale/raise/invalid、拓扑错误
分类、单次冻结读取和 A3→A2 锁序。项目 Python 3.12 下 65 项 task-state 定向测试、
189 项直接相关 HTTP/stream/A3/反馈测试及全仓 1129 项回归通过。

## 3.3.6 跨出口 parity 与回归验收（DONE；尚未部署）

3.3.6 用非空 standalone A2/A3 V1 验证 `/api/session`、HTTP 200 业务 JSON、
受控 HTTP error、五条 stream 的 result/error 和 reset。相同入口能力下，JSON 与
stream 必须返回完整 `to_dict()` 同值快照，而不是只比较 phase；JSON/session/error
位于根级，stream result 位于 `event.data`，stream error 位于事件根级。图片成功入口
保留 `trusted_image_event=true` 的能力差异，其他入口为 false。

公开状态只接受“非空 legacy + exact `TaskStateSnapshotV1`”这一完整冻结 pair。
all-missing、legacy-only、projection-only、typed-only 及其他不完整组合一旦证明 read-set
已尝试，就不得事后读取 live state 或拼接不同时间点的两半；应按可信 runtime 拓扑
零读取降级为脱敏 `INCONSISTENT`。该规则同时覆盖业务 JSON/stream、`/api/session`
和 reset 清理异常。业务结果缺 projection 仍使成功出口 fail-closed；若错误终态已持有
可信 legacy/V1 pair，可复用该 pair，但不得把 projection 当成新的状态来源。

Response Store finalization 或结果序列化在非空业务 V1 形成后失败时，JSON error 与
stream error 复用同一冻结 V1，且不再捕获。未传 `task_state_capabilities` 的
`prepare_units()` / `handle_crop()` 保持 legacy 行为，明确零 V1 capture；busy/queue、
progress、无会话输入拒绝和非任务路径继续不增加状态。

验收新增独立 `tests/test_task_state_exit_parity.py`，并补 A3 legacy 兼容断言。项目
Python 3.12 下 73 项 task-state 定向测试、179 项 FastAPI/A3/Response Store 直接
相关测试及阶段最终全仓 1147 项回归全部通过。未部署、未重启任何服务。

## 3.4.1 前端 V1 解析与 fail-closed model（DONE；尚未接线/部署）

前端新增 `tiku_agent/demo_web/task_state.js` 纯模块，按 V1 exact key、schema、类型、
枚举、phase/status/next-stage、phase action 子集、completed-step 可达集合、candidate
generation 及父子/unit 拓扑重建并深冻结快照。解析不修改输入；missing、未知 schema、
任意 shape/type/enum/topology 错误返回无 snapshot、零动作的稳定 model。服务端合法
`INCONSISTENT` 保留只读快照和 next-stage 供后续展示，但同样关闭全部动作；模块私有
WeakSet 只允许自身创建的 model 进入 `allowsWorkflowAction()` / `allowsChildAction()`，
伪造对象和原型继承对象均不能授权。

exact shape 只接受 JSON 可表达的可枚举 data property 和标准稠密数组；accessor、
非枚举必需字段/数组项、Symbol/额外数组属性及非标准数组原型均拒绝。校验器从 property
descriptor 一次性捕获值后再解析，避免同一字段重复求值造成 phase/action 校验与最终
投影不一致。

敏感文本是否可公开仍以 Python typed/public mapper 为唯一边界；浏览器只重复两端语义
稳定的 Unicode code-point 长度和控制字符检查，不手抄 Python `re` 的 Unicode
`\b`/`\s` 规则。脚本以 classic `defer` 先于 `demo.js` 加载；3.4.1 收口时仅初始化
MISSING/closed context，没有消费信封或改变 A2/A3 按钮、自动打开、旧 phase/unit flag
及 `a3:null` 兼容行为。信封消费由下一批 3.4.2 独立完成。

`tests/test_demo_web_task_state.py` 使用 Python typed contract 生成 canonical empty、
standalone A2、A3 active、父/子 `INCONSISTENT` 及全部合法 phase/action 矩阵，再由真实
Node/CommonJS 和 browser-global 分支执行前端模块；同时覆盖缺/多键、未知 schema/phase/
action、非法里程碑、revision/generation、unit 排序/重复/双 ACTIVE、父子错绑、Unicode
边界、accessor/descriptor exact shape 和伪造 model。93 项前端/FastAPI 与 73 项
task-state 回归通过；全仓发现的 1150 项中 1 项无关诊断 WAL 顺序测试失败，该失败在
`0f4bd8e` 的 1147 项基线中同样复现且单独运行通过。未部署、未重启任何服务。

## 3.4.2 前端响应信封接线（DONE；尚未部署）

`demo.js` 以精确 JSON/stream 任务路径白名单在两个公共请求包装器集中接线：session、
普通 JSON success/error 与 reset 从根级读取，stream result 只从 `event.data` 读取，
stream error 只从事件根级读取。响应先由 `task_state.js` 从可枚举 data property 捕获并
完整校验、重建 model，成功后才单次替换 context；缺失、未知 schema、非法 descriptor/
shape/topology 及合法 `INCONSISTENT` 均保持 fail-closed，不回退寻找错层字段。

每个任务状态请求开始即切到 MISSING/closed，并取得一次性 Symbol token；只有最新请求
可提交一个终态，旧请求、重复终态和已结束 token 均不能读取或覆盖新 model。两个包装器
在 `finally` 统一退休 token。`QUEUE_FULL`/`QUEUE_TIMEOUT`、progress 与非任务响应严格
no-update；queue 即使意外夹带状态也忽略。重连在 health 前后检查 busy，观察型 session
refresh 不能抢占正在执行任务的 post-media-reopen 最终状态。

测试以 Python typed fixture 驱动真实 Node 模块，并动态执行 `demo.js` 的实际白名单与消费
函数，覆盖 session、JSON、五条 stream、reset、missing/invalid、queue、非任务、错层、
乱序、重复终态、getter 零读取和 busy 重连竞态；对抗复核未发现遗留高/中风险。95 项
前端/FastAPI 与 73 项 task-state 回归通过；全仓 1152 项仅既有 SQLite WAL 顺序测试
失败，该项单独通过。未迁移任何按钮，未部署、启动、停止或重启服务。

## 3.4.3 A2 子题动作授权迁移（DONE；尚未部署）

候选卡 `select_candidate`、业务 `retry_search` 及携带候选 action context 的 transport
retry 只经 branded `allowsChildAction()` 放行，不再使用 legacy session phase 授权。
按钮保存 V1 child 的 `task_id`、`task_revision` 和 candidate generation；候选再绑定
rank，并按 V1 candidate count 校验。候选请求的 revision/generation 也直接来自同一
V1 binding，legacy session 投影仅保留展示和兼容用途。动作在渲染、状态同步和点击前
均重验；旧历史缺 binding、跨 child/revision/generation、越界 rank、媒体失效、busy、
missing/invalid/未知 schema/`INCONSISTENT` 以及只有 `next_stage` 均 fail-closed。

`begin()`、终态消费、`finally`、busy 和消息渲染会同步已有 A2 按钮；未授权的业务恢复
按钮隐藏，普通文本 retry、登录、上传、反馈及本地媒体查看不进入 child 动作命名空间。
Python typed fixture 驱动的 Node 动态矩阵同时覆盖 V1/legacy token 矛盾、candidate retry
binding、按钮恢复/关闭和 queue no-update；96 项前端/FastAPI 回归通过。A3 控件、自动
打开逻辑和服务端状态机未改，未部署、启动、停止或重启服务。

## 后续批次

1. **3.2.1 纯构造器（DONE）**：已完成从冻结 read-set 和可信入口证据到 V1 快照的无 I/O 投影，实现与定向测试见 `tiku_agent/task_state_builder.py` 和 `tests/test_task_state_builder.py`。
2. **3.2.2 锁内权威读取（DONE）**：已在 A3→A2 锁序内一次读取父子状态，在 standalone A2 锁内单读 child，并为已持锁调用方提供零重锁、零 store 读取的 frozen-state 入口；缺失/不可读/稳定未知分类、受控文件与入口能力证据及回归测试见 `tiku_agent/task_state_runtime.py`、两个 runtime 类和 `tests/test_task_state_runtime.py`。项目 Python 3.12 下 47 项 task-state 定向测试及全仓 1031 项回归通过。
3. **3.2.3 异常与矩阵测试（DONE）**：父 route/phase 与 child phase/topology 的实际构造矩阵、A3 active/组合读取异常、脱敏及旧 revision 动作证据均已补齐；阶段 3.2 整体完成。项目 Python 3.12 下 51 项 task-state 定向测试及全仓 1035 项回归通过。
4. **3.3 出口一致性（DONE；尚未部署）**：3.3.1～3.3.6 已完成；跨 session、JSON success/error、stream result/error 和 reset 的 exact V1 parity、失败后处理、零重读及 legacy 兼容均已验收，阶段最终全仓 1147 项通过。
5. **3.4 前端消费（IN_PROGRESS；尚未部署）**：3.4.1 纯解析/model、3.4.2 原子信封接线与 3.4.3 A2 动作迁移已完成；下一步迁移 A3 workflow/unit 控件，只用服务端 `allowed_actions` 授权，`next_stage` 仅供展示/引导，不授权动作。
6. **3.5 启用门（尚未实现）**：定向/全量回归、8896 契约烟测、只读 live 对照后再精确启用 8790；不触碰 8788/8794/8795。
