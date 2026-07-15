# 主线观测分线：轨迹界面可用性补充

## 0. 优先级

本文是 `15_product_mainline_mirror_spec.md` 的优先补充。两者冲突时，以本文为准；15 中的主线镜像、行为一致性、透明观测、隔离和差分测试要求全部继续有效。

本补充只调整右侧轨迹界面的呈现和人工复核方式，不改变原始 trace schema、采集范围或主线业务行为。

## 1. 核心区分：机器完整轨迹与人工复核队列

右侧必须明确分成两个概念，不能把完整机器日志直接当成人工打分清单。

### 1.1 机器完整轨迹

机器仍然按真实调用顺序保存全部事件，包括：

- `turn_started（回合开始）`
- `intent_decided（意图判断）`
- `authorization_checked（权限校验）`
- `tool_started（工具开始）`
- `tool_completed（工具结果）`
- `state_transition（状态变化）`
- `turn_completed（回合完成／最终结果）`

这些事件用于复现、调试、自动检查和验收主线一致性。原始 JSONL 中继续保留英文 `event_type`，不得为了中文显示修改存储协议。

### 1.2 人工复核队列

人工复核队列是从完整轨迹中筛出的关键判断，不等于完整轨迹。默认只显示：

1. `intent_decided（意图判断）`
2. `tool_completed（工具结果）`
3. `turn_completed（最终结果）`

用户可以对这些关键项按需选择：

- `correct（正确）`
- `incorrect（错误）`
- `uncertain（不确定）`
- 不操作，继续保持 `unlabeled（未复核）`

界面必须明确写出：

> 只标你想核对的关键项，不需要逐条评分；未标记不代表正确或错误。

不得出现“待完成 3/10”“还有 7 项未评分”等会暗示必须全部完成的文案或进度条。

## 2. 默认显示规则

### 2.1 默认展开的关键项

#### `intent_decided（意图判断）`

显示：

- 最终 action 的英文名和中文解释；
- source 的英文名和中文解释；
- question_index / candidate_rank / chapter 等与本轮相关的结构化参数；
- clarification/reject 原因；
- 人工三态标签。

不显示用户原文、完整 prompt、模型原始响应或思维链。

#### `tool_completed（工具结果）`

每个真实完成的工具各显示一项，显示：

- 工具英文名和中文解释；
- `ok（是否成功）`；
- `next_state（工具建议的下一状态）`；
- 白名单输出摘要，如单题/多题、章节、荷载数量、route、结构类型、候选数量、rerank 是否完整、答案数量；
- error_kind；
- 人工三态标签。

`tool_started（工具开始）` 不另占默认复核卡片；其开始时间、输入摘要和配对关系放在对应工具结果的技术详情里。

#### `turn_completed（最终结果）`

每个回合显示一项，显示：

- response_type 的英文名和中文解释；
- 最终 phase；
- intent；
- 候选数、答案数；
- 自动检查是否发现问题；
- 人工三态标签。

当 response_type 为 `no_match（未找到匹配）` 时，额外突出 NO_MATCH 分类：

- `reasonable_no_match（合理无结果：题库确实没有）`
- `false_no_match（错误无结果：题库有但 Agent 没找到）`
- `uncertain_no_match（暂时无法判断）`

NO_MATCH 不能自动标红，也不能自动算 Agent 错误。

### 2.2 默认折叠的技术详情

以下事件默认放进 `技术详情（完整机器轨迹）` 折叠区：

- `turn_started（回合开始）`
- `authorization_checked（权限校验）`
- `state_transition（状态变化）`
- `tool_started（工具开始）`

正常情况下，它们主要供调试和验收查看，不要求用户逐项贴标签。

技术详情区应提供：

- 按 sequence 排列的完整事件；
- 每类事件的英文名和中文解释；
- 事件数量；
- 自动检查状态；
- 展开单条查看脱敏 payload。

## 3. 异常时的自动提升

默认折叠不等于隐藏异常。出现以下情况时，相关技术事件必须自动展开或在复核队列顶部提醒：

- `authorization_checked（权限校验）` 结果异常、缺失或与主线基准不一致；
- `state_transition（状态变化）` 的 `automatic_check=fail`；
- `tool_started（工具开始）` 没有配对的 `tool_completed（工具结果）`；
- sequence 不连续；
- turn_started/turn_completed 缺失或重复；
- scan 返回该回合相关 issue；
- 主线差分测试发现授权、状态或工具序列不一致。

异常提示使用：

`英文 code（中文解释）`

例如：

- `authorization_missing（缺少权限校验记录）`
- `tool_pair_mismatch（工具开始与结果未配对）`
- `embedded_automatic_check_failed（状态自动检查失败）`

即使自动提升，这些技术事件也不自动变成人工必评项。用户可以查看并按需标记关联的关键事件。

## 4. 事件数量不是固定值

界面和文案必须说明：

> 每个回合的轨迹数量不固定，取决于本轮调用了多少工具、发生了多少次状态变化。

不能写“一个回合固定 10 条”或用固定槽位渲染。

示例：

```text
简单问候
├─ turn_started（回合开始）
├─ intent_decided（意图判断）
├─ authorization_checked（权限校验，实际次数遵循主线）
└─ turn_completed（回合完成／最终结果）

单题搜索
├─ turn_started（回合开始）
├─ intent_decided（意图判断）
├─ authorization_checked（权限校验，实际次数遵循主线）
├─ 多个 tool_started/tool_completed（工具开始／工具结果）
├─ 多个 state_transition（状态变化）
└─ turn_completed（回合完成／最终结果）
```

授权事件数量也以主线真实调用为准，不为满足 UI 固定数量而人工补造。

## 5. 双语显示规范

右侧 UI 中所有英文事件名必须显示为：

`英文（中文解释）`

不得只显示英文，也不得只显示中文而失去与原始 trace 的对应关系。

### 5.1 事件名固定映射

| 原始值 | UI 显示 |
|---|---|
| `turn_started` | `turn_started（回合开始）` |
| `intent_decided` | `intent_decided（意图判断）` |
| `authorization_checked` | `authorization_checked（权限校验）` |
| `tool_started` | `tool_started（工具开始）` |
| `tool_completed` | `tool_completed（工具结果）` |
| `state_transition` | `state_transition（状态变化）` |
| `turn_completed` | `turn_completed（回合完成／最终结果）` |

未知事件使用：

`<原始英文值>（未知事件）`

不得因为没有中文映射而丢弃事件。

### 5.2 工具名建议映射

工具结果卡也应双语显示：

| 工具 | UI 显示 |
|---|---|
| `analyze_multi_image` | `analyze_multi_image（判断单题或多题）` |
| `prepare_question_units` | `prepare_question_units（拆分并准备多题）` |
| `analyze_image` | `analyze_image（识别题图、章节与荷载）` |
| `route_bank` | `route_bank（选择主库或字母库）` |
| `classify_structure` | `classify_structure（识别结构类型）` |
| `coarse_search` | `coarse_search（题库粗筛）` |
| `global_search` | `global_search（全章节严格搜索）` |
| `rerank_candidates` | `rerank_candidates（候选视觉复筛）` |
| `answer_candidate` | `answer_candidate（获取候选答案）` |

未知工具同样显示 `<原始英文值>（未知工具）`。

### 5.3 常用结果值

action、source、phase、response_type、verdict 和 scan code 第一次出现时也应尽量使用英文加中文解释，例如：

- `context_llm（上下文模型判断）`
- `validator（代码校验器）`
- `WAIT_CHAPTER（等待章节）`
- `NO_MATCH（未找到匹配）`
- `unlabeled（未复核）`

原始英文值必须可见，方便用户把 UI 与 JSONL、验收报告对应起来。

## 6. 页面结构修订

右侧 `当前回合` 页签调整为：

```text
当前回合
├─ 提示：只标关键项，不需要逐条评分
├─ 人工复核队列
│  ├─ intent_decided（意图判断）
│  ├─ tool_completed（工具结果） × 实际工具数
│  └─ turn_completed（最终结果）
└─ 技术详情（完整机器轨迹） [默认折叠]
   └─ 按 sequence 显示全部事件
```

复核队列顶部显示：

- `关键项 X 条`：只是当前可复核项数量；
- `已复核 Y 条`：仅作信息，不显示完成百分比；
- `其余保持 unlabeled（未复核）即可`。

不得使用红点催促、必填星号、提交整份评分、完成率或阻止进入下一回合。

## 7. API 与存储不变原则

- `/api/turns/{turn_id}` 仍返回该回合完整事件，不能只返回复核队列。
- 前端在完整事件上做视图分组；后端也可额外返回 `review_items`，但不得替代或删减 `events`。
- `traces.jsonl` 继续完整保存所有真实事件。
- `labels.jsonl` 只保存用户实际提交的标签；未点击的事件不生成默认标签。
- summary 的 unlabeled 继续保留，不因 UI 折叠技术事件而自动算 correct。
- 人工标签不反向影响主线 Agent、trace 或自动 scan。

## 8. 验收补充

以下任一项不满足即 FAIL：

1. 右侧出现只含英文的事件名。
2. 原始 trace 缺少技术事件，只为减少 UI 条目而停止采集。
3. 默认复核队列包含每一条 turn_started、authorization_checked、state_transition 或 tool_started，并暗示用户逐项评分。
4. 自动检查异常仍被折叠且没有提醒。
5. 将事件数写死为 10 条或其他固定数量。
6. 未标记事件被自动写成 correct、incorrect 或 uncertain。
7. 用户必须完成所有标签才能继续搜题、换回合、查看结果或关闭页面。
8. NO_MATCH 被自动判错，或没有 reasonable/false/uncertain 三分类。
9. 技术详情不能查看完整 sequence 和脱敏 payload。
10. 工具开始/结果无法配对查看，或 tool_completed 没有对应中文解释。

必须通过的界面场景：

- 问候回合：复核队列主要显示意图和最终结果，技术详情可看到完整机器事件。
- 正常搜题回合：工具数动态变化，复核队列显示实际 tool_completed 数量，无固定“10 条”。
- 状态检查失败：对应 state_transition 自动提醒/展开，但不强迫用户评分。
- NO_MATCH：最终结果卡显示三分类，默认仍为 unlabeled。
- 未知事件/工具：保留英文原值并显示“未知事件/未知工具”，不丢数据、不崩页面。

## 9. 一句话产品口径

> 机器负责完整记录每一步，界面只把真正需要人判断的关键结论递给用户；技术过程随时可查，异常主动提醒，但用户永远不需要逐条批改整份日志。
