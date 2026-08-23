# 工具反馈第一阶段审计

## 目标与范围

本阶段以生产基线 `1102553` 为起点审查工具反馈，并在新 Agent / Web 分支完成第一批修复。
不实现看门狗，不让模型参与错误处理，也不改正常候选、答案和原模型润色文案。

目标是回答三个问题：

1. 工具现在实际返回什么；
2. 哪些内部字段会穿透成用户文字；
3. 怎样补齐结构化反馈，让后续模型只能接触安全事实。

审查范围包括 `tiku_agent/tool_result.py`、`tiku_agent/tools.py`、Agent 编排、A3 转发和
HTTP/Web 出口。共享 `RuleRouter` 的 CLI、飞书和旧 GUI 使用点只做影响核对；飞书是个人内部入口，
本阶段明确不改。媒体持久化仍属于相邻边界；HTTP 异常映射已补齐，但不把它伪装成工具层问题。

## 结论

审计基线并不是“完全没有结构化反馈”。`ToolResult` 已有 `outcome`、`code`、`retryable`、
`error_category`、`next_state`、`action` 和 `data`。真正缺少的是公开与内部信息的强制分层：

- 基线中的 `error` 同时承担内部原因、降级说明和用户文案；
- `data` 同时装业务结果、路径、模型原始字段和可公开事实；
- 编排层没有 `code -> 安全中文语义` 的权威映射，多个分支直接使用 `error`；
- 工具代码、事实键和恢复动作没有注册校验，未知值没有统一安全回退；
- 因此现有结构适合内部编排和日志，但不足以直接驱动用户回复或模型润色。

用户见到的 `mixed symbolic and numeric load` 不是模型偶发输出，而是这项契约缺口的确定性结果。

## 本地第一阶段修复状态（2026-08-23）

已在当前 Agent 主线完成本地修复，尚未启动服务、切换端口或上线：

- `ToolResult` 增加独立的 `safe_facts`，非成功工厂支持传入 `action`；旧 `error` 和旧构造方式保留兼容。
- `route_bank_tool` 按稳定的 `RuleRouter.category` 区分混合荷载和空/未知荷载，分别返回
  `LOAD_ROUTE_MIXED_REVIEW_REQUIRED` 与 `LOAD_ROUTE_INPUT_UNUSABLE`，并附带审查过的事实和
  `retry_upload` 动作；不再按英文 `reason` 做业务判断。
- Agent 的错误、需要补充信息、部分成功和无匹配出口统一经过确定性 `code -> 中文安全文案`
  映射；未知 code 按结果状态固定回退，绝不读取 `result.error`、`data` 或动态 `rerank_note`。
- `ToolResult.to_public_dict()` 只公开五态、白名单 code、注册表决定的恢复元数据和格式合法且不含
  凭据特征的请求 ID；`error`、`data`、`safe_facts` 和内部诊断不会进入公共序列化。
- HTTP JSON、流式错误、流式进度和 A3 会话快照都使用白名单投影。正常已知进度文案原样通过，
  异常文本按 `stage` 回退固定中文；A3 内部 reason、confidence、路径等继续保留但不公开。
- A3 回到 A1/A2 时显式返回 `a3: null`，裁剪审查只公开有限错误码和对应固定中文，避免旧页面状态残留。
- 已覆盖混合荷载真实工具链、旧构造兼容，以及注入英文、路径、Traceback、凭据特征和伪造协议
  元数据时的安全回退。飞书入口及其运行服务均未修改、未启动、未重启。

本地验证：Python 语法检查、Web JavaScript 语法检查、147 项聚焦测试和全仓 `785/785` 测试通过。
测试中的复筛超时、缺文件等 WARNING 来自既有模拟用例，不是测试失败。下一步先由用户
审查本阶段结果；不因结构层完成就自动接入模型润色、看门狗或部署。

## 审计基线 ToolResult 契约

| 字段 | 基线含义 | 主要缺口 |
| --- | --- | --- |
| `outcome` | 五态结果 | 已规范化，但 `NEEDS_INPUT`、`PARTIAL` 仍被旧 `ok=True` 兼容语义覆盖，调用方容易误判 |
| `code` | 工具结果码 | 无格式和注册校验；同一业务原因可能共用过宽代码 |
| `error` | 自由文本 | 语义过载且没有安全等级，是基线直接穿透的核心原因 |
| `error_category` | 粗粒度内部类别 | `NEEDS_INPUT`、`NO_MATCH` 通常为空，且不能独立生成具体用户说明 |
| `data` | 工具业务数据 | 未区分私有数据与公开事实，包含路径、模型 reason、候选详情等 |
| `next_state` | 建议后续状态 | 自由字符串；部分调用点并不应用它，不能视为真实状态 |
| `action` | 建议恢复动作 | 基线构造器不能传入，生产工具基本都是空动作 |
| `retryable` | 是否可重试 | 可用，但没有保证与 `action`、用户文案一致 |
| `request_id/search_id` | 请求关联 | 工具通常不填写，主要由外层协议补齐 |

`to_dict()` 会序列化 `error` 和完整 `data`。它目前主要用于内部脚本和测试，但不能作为未来
模型输入或公共 API 数据使用。本阶段保留它作为内部兼容接口，另增白名单公共序列化，不混用两者。

## 审计基线九个生产工具

| 工具 | 非成功结果 | 基线公开风险 | 审计结论 |
| --- | --- | --- | --- |
| `analyze_image` | 缺章节、识别失败 | `error` 为固定中文，但 `data` 含图片路径和模型分类原文 | 当前文案安全；仍需公开事实白名单 |
| `analyze_multi_image` | 多题判断降级 | 固定中文通过 PARTIAL notice 直出 | 当前内容安全，但依赖自由文本约定 |
| `prepare_question_units` | 识别失败、裁图降级 | 原异常被丢弃，公开文字固定；`data` 含裁图路径 | 当前内容安全；路径不得进入模型 |
| `route_bank` | 荷载需复核、路由失败 | 基线中 `route.reason` 可为英文，并同时进入 `data.reason` 和 `error`；本地第一阶段已在 Agent 出口隔离 | **P0，已确认真实泄漏；本地 Agent 已修复** |
| `classify_structure` | 缺图、类型不确定、模型降级 | 公开文字固定；`data.reason` 可能来自模型 | 当前用户文本安全；未来模型输入不能使用完整 `data` |
| `coarse_search` | 章节不存在、无匹配、失败 | 固定中文或空 `error`；`data` 含候选路径 | 当前内容安全；缺明确恢复动作 |
| `global_search` | 缺图、不支持、部分完成、无匹配、失败 | 固定中文；缺图和失败动作仍为空 | 当前内容安全；协议与动作不完整 |
| `rerank_candidates` | 缺图降级、无可靠候选、复筛降级、失败 | PARTIAL/NO_MATCH 会把 `error` 或 `rerank_note` 直接交给编排层 | 当前内置值大多安全，但边界会信任任意注入文本 |
| `answer_candidate` | 编号错误、读取失败、无答案 | NO_MATCH 的 `error` 直接公开；成功只表示找到/复制路径 | 当前内置值安全；不能证明 HTTP 媒体已交付 |

`parse_candidate_action_tool` 当前只有测试调用，不在生产 `AgentToolbox` 中。它也使用自由文本
`error`，迁移契约时应同步处理，但不应把它算成线上英文问题的来源。

### 非成功 code 清单

| 工具 | NEEDS_INPUT | PARTIAL | NO_MATCH | ERROR |
| --- | --- | --- | --- | --- |
| `analyze_image` | `CHAPTER_REQUIRED` | - | - | `IMAGE_ANALYSIS_FAILED` |
| `analyze_multi_image` | - | `MULTI_DETECTION_FALLBACK` | - | - |
| `prepare_question_units` | - | `MULTI_CROPS_UNAVAILABLE` | - | `MULTI_DETAIL_INVALID`、`MULTI_DETAIL_FAILED` |
| `route_bank` | `LOAD_ROUTE_MIXED_REVIEW_REQUIRED`、`LOAD_ROUTE_INPUT_UNUSABLE`（兼容旧 `LOAD_ROUTE_NEEDS_REVIEW`） | - | - | `BANK_ROUTE_FAILED` |
| `classify_structure` | - | `STRUCTURE_FILTER_SKIPPED_NO_IMAGE`、`STRUCTURE_TYPE_UNCERTAIN`、`STRUCTURE_CLASSIFICATION_FALLBACK` | - | - |
| `coarse_search` | `UNKNOWN_CHAPTER` | - | `NO_COARSE_CANDIDATES` | `COARSE_SEARCH_FAILED` |
| `global_search` | `GLOBAL_SEARCH_IMAGE_REQUIRED` | `GLOBAL_RERANK_INCOMPLETE` | `NO_GLOBAL_COARSE_CANDIDATES`、`NO_GLOBAL_RELIABLE_CANDIDATES` | `GLOBAL_SEARCH_UNSUPPORTED_ROUTE`、`GLOBAL_SEARCH_FAILED` |
| `rerank_candidates` | - | `RERANK_SKIPPED_NO_IMAGE`、`RERANK_INCOMPLETE_COARSE_FALLBACK`、`RERANK_EMPTY_COARSE_FALLBACK` | `NO_CANDIDATES_TO_RERANK`、`NO_RELIABLE_RERANK_CANDIDATES` | `RERANK_FAILED` |
| `answer_candidate` | `CANDIDATE_RANK_INVALID` | - | `ANSWER_FILES_NOT_FOUND` | `ANSWER_LOOKUP_FAILED` |

其中只有 `LOAD_ROUTE_NEEDS_REVIEW` 在基线直接使用共享路由的英文动态原因；但复筛降级会使用
`rerank_note`，候选编号错误会把数字插入自由文本，其他 code 也都依赖“开发者自觉只写安全中文”而
不是契约校验。

## 已确认的穿透链路

### 混合数字与字母荷载

```text
classify_loads
  category = mixed_symbolic_numeric
        |
RuleRouter.route
  reason = "mixed symbolic and numeric load"
        |
route_bank_tool
  NEEDS_INPUT / LOAD_ROUTE_NEEDS_REVIEW
  error = route.reason
        |
TikuSearchAgent._stop_for_tool_result
  text = result.error
        |
A3 原样转发 -> HTTP 原样返回
```

修复前的真实结构是：

```text
intent=clarification
status=NEEDS_INPUT
code=LOAD_ROUTE_NEEDS_REVIEW
action=""
text=mixed symbolic and numeric load
```

已用当前生产 Python、真实 `route_bank_tool` 和无模型 Agent 调用动态复现以上结果。工具声明的
`next_state=WAIT_INPUT` 没有成为 Agent 状态，实际 `phase=READY_TO_ROUTE`；`action` 仍为空。
空荷载或无法分类的荷载还会在基线沿相同链路公开另一句英文
`empty, unknown, or unsupported load`。

现有测试只断言了 `outcome/code`，Agent 测试又使用预先写好的中文假结果，所以没有覆盖真实工具到
HTTP 的链路。

### 其他自由文本入口

基线 Agent 还会在以下场景信任工具字符串：

- `NEEDS_INPUT`：直接把 `result.error` 作为回复；
- `PARTIAL`：把 `result.error` 或 `data.rerank_note` 追加到正常回复；
- `NO_MATCH`：复筛和答案分支直接使用 `result.error`；
- `ERROR`：错误文字进入状态，用户追问失败原因时，未知内容可能被清洗后重新展示。

所以只把混合荷载那一句英文改成中文能修复当前样本，却不能补上边界。

## 相邻入口

共享 `RuleRouter` 还被其他入口使用：

- 飞书先给中文提示，但会公开内部类别 `mixed_symbolic_numeric`；
- CLI 会输出 `needs_review: not searching any bank`；
- 旧 GUI 会展示 `category: reason`，因此直接显示英文；
- 入库流程把 `category/reason` 当内部决策数据使用，不应改成用户文案。

本阶段决定是：只改新 Agent 的工具反馈与 HTTP/Web 公共边界，飞书、CLI 和旧 GUI 保持原样。
实现只读取共享路由的稳定内部分类，没有修改 `RuleRouter` 的返回契约，因此不会间接改变飞书。
若以后要治理这些旧入口，应另开范围；入库流程仍必须使用稳定内部分类，不能依赖中文文案做逻辑判断。

## 推荐的新分层

### 1. 工具内部结果

工具只报告事实和决策，不负责写最终中文：

```json
{
  "outcome": "NEEDS_INPUT",
  "code": "LOAD_ROUTE_MIXED_REVIEW_REQUIRED",
  "next_state": "WAIT_INPUT",
  "retryable": false,
  "action": "retry_upload",
  "safe_facts": {
    "load_representation": "mixed",
    "automatic_search_supported": false
  }
}
```

内部诊断单独保存，不进入公共序列化：

```json
{
  "reason_code": "MIXED_SYMBOLIC_NUMERIC",
  "load_class_counts": {
    "symbolic_unassigned": 1,
    "numeric": 1
  }
}
```

原始异常、路径、`load_details`、模型 reason 和供应商返回只能留在 `data` 或内部诊断，不能进入
`safe_facts`。

### 2. 确定性安全语义

编排层使用一个小型注册表按 `code + safe_facts` 生成安全回复草稿：

```text
TOOL_FEEDBACK_CATALOG[code]
  -> 期望 outcome
  -> 允许的 safe_facts 键
  -> 默认 action
  -> 固定中文 renderer
```

未知 code、字段不匹配或 renderer 失败时，按 outcome 使用固定回退，绝不回退到 `error`。

混合荷载的确定性语义建议为：

> 识别到题中同时包含数值荷载和未赋值的字母荷载，当前题库暂时不能可靠自动检索。你可以换一张题图，或联系作者人工查找。

当前没有“让用户修正荷载类型”的状态和工具，因此不能生成“请确认是数字还是字母”这种伪操作。
自动拆成两次跨库检索也没有经过验证，不应作为降级方案。是否显示“联系作者”由编排层根据当前
入口是否真的提供人工联系方式决定，工具不能自行声明。

### 3. 可选模型润色

模型只能接收安全草稿。已知 code 的确定性中文可以直接发送；只有真实样本证明表达仍需改善时，
才调用模型：

```json
{
  "code": "LOAD_ROUTE_MIXED_REVIEW_REQUIRED",
  "safe_text": "...经过审查的中文语义...",
  "safe_facts": {"load_representation": "mixed"},
  "allowed_actions": ["retry_upload"]
}
```

禁止传入 `ToolResult.error`、完整 `data`、内部诊断、异常、路径或模型原始 reason。模型只能改表达，
生成后必须校验 code、事实和动作没有变化；失败时返回确定性中文草稿。

### 4. 最终薄看门狗

看门狗只保留为最后保险，检查路径、堆栈、凭据、原始 JSON、控制字符及明确事实矛盾；不再负责从
一段未知文本反推业务错误类型。

## ToolResult 最小演进方案与状态

不恢复实验中的全量 `user_output`。建议小步演进现有结构：

1. 已增加 `safe_facts`，并由具体反馈 renderer 选择允许使用的事实；公共序列化不直接输出它。
2. 非成功构造器已可显式接收 `action`。
3. 公开工具 code 白名单已全部登记到共享协议表，并由测试校验 code、状态、层级和恢复元数据；未知
   code 仍按五态统一回退。
4. `error` 保留为内部兼容字段，Agent 和 HTTP/Web 公共出口不再直接读取它。
5. 原始诊断仍留在现有内部字段；是否拆成专门诊断对象留给后续，不阻塞本阶段。
6. `next_state` 继续作为内部管线提示；公开恢复动作由注册表或状态回退决定，不信任任意调用方字段。
7. 已增加确定性工具反馈 renderer，未知 code 固定回退。
8. 正常 SUCCESS 路径、现有安全文案和原模型润色保持原样。

第一批选择迁移所有会公开 `result.error` 的调用点，并优先注册：

- 混合荷载 `LOAD_ROUTE_MIXED_REVIEW_REQUIRED` 与空/未知荷载 `LOAD_ROUTE_INPUT_UNUSABLE`；
- 缺章节、缺题号、缺候选号和编号越界；
- 复筛部分完成与无可靠候选；
- 答案文件不存在和答案读取失败；
- 通用工具失败的固定安全回退。

## 不属于工具契约的两个问题

### 媒体交付

`ANSWER_FILES_FOUND` 只能证明工具找到或复制了文件，不能证明 HTTP 已成功持久化并返回媒体 URL。
候选和答案的成功文案仍必须在媒体边界按实际交付数决定：候选图片应整组原子交付；答案 0 张、
部分和全部交付必须区分。

### HTTP 与流式异常

HTTP JSON 响应和流式错误事件已按 `RequestProtocol.code` 映射固定安全文字，不再公开传入的
`detail` 或 `str(exc)`；未知 code 按协议状态回退到固定中文。原始异常仍只应进入内部日志。

## 实施与验收顺序

1. 已增加反馈注册表、混合荷载细分 code 和固定状态回退，不接模型。
2. Agent 的 NEEDS_INPUT/PARTIAL/NO_MATCH/ERROR 已从读取 `error` 改为确定性安全反馈。
3. 已用真实工具链验证 `q + 10kN`，并保留 `P=40 + 2P` 走主库、`q + P` 走字母库的边界测试。
4. 已注入英文异常、路径、Traceback、凭据特征和未知协议，覆盖非成功出口的安全回退。
5. 已处理 HTTP JSON、流式进度/错误和 A3 会话快照的公共投影；媒体交付仍是独立后续问题。
6. 飞书、CLI 和旧 GUI 明确排除，不作为本阶段缺口，也不随本阶段部署。
7. 先观察真实异常样本；只有确定性安全文案仍解决不了表达问题时，才评估受控模型润色或薄看门狗。

本阶段已按以下标准完成：审计表覆盖生产工具、真实英文链路可解释、公共字段边界明确，
并且没有把媒体或 HTTP 问题错误归因给工具层。部署、模型润色和看门狗不属于本阶段完成条件。
