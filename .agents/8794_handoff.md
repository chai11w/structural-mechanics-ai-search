# 8794 候选线路交接文档

> Claude Code 负责实现，Codex 负责审查。每完成一步更新本文件并提交。
> 审查流程：读本文件 → 按"改动文件"清单逐个看 git diff → 运行"验证命令"确认结果。

## 当前进度

- **目标**：8794 状态感知安全回答（roadmap Stage 4）——让安全回答能感知当前业务阶段（等待章节/候选选择/答案展示），业务操作仍走固定状态机。
- **总计划**：M1 状态摘要模块 → M2 prompt 契约 → M4 generator+agent 接线 → M3 接线后按需兜底文案 → M5 runtime 验收测试 → M6 离线状态感知矩阵评估 + 反射指令强化。
- **已完成**：M1–M6 全部完成；Codex 复核完成 R1 状态矛盾输出校验、R2 内部 action 中文翻译、R3 模型白名单/代码校验事实拆分与 R5 两项误杀精修。状态感知安全回答已具备严格六字段摘要、真实模型生成、代码级高置信度矛盾拦截和用户语义下一步。
- **下一步**：继续讨论剩余审查建议；问题收敛后再交 8793 评审并提升 8790。阶段专用安全寒暄兜底经讨论暂不增加，沿用现有业务阶段提示与通用安全兜底。

---

## 已完成步骤

### R6 同题答案返回后允许改选候选（真实评审修复）
8793真实评审发现：首次选择候选并返回答案后，业务阶段进入`ANSWERED`，网页候选安全门却只允许`WAIT_CANDIDATE_CHOICE`，导致同一道题、同一候选批次的其他候选被误判为上一题旧候选。请求在进入Agent前即被拒绝，因此8793右侧也没有新回合，停留在上一次答案评审。

前后端现都允许`WAIT_CANDIDATE_CHOICE/ANSWERED`两阶段使用候选按钮，但仍必须同时满足会话有效、`task_revision`一致、`candidate_generation`一致且排名有效。上传新题或生成新候选批次后，旧按钮仍严格拒绝。新增后端同批次改选、旧批次拒绝和Agent已回答后改选完整链路测试，更新前端资产版本避免浏览器旧缓存；相关67项、JS语法与全量323项通过。

改动文件：
- `tiku_agent/fastapi_demo.py`
- `tiku_agent/demo_web/demo.js`
- `tiku_agent/demo_web/index.html`
- `tests/test_tiku_agent_fastapi_demo.py`
- `tests/test_tiku_agent_agent.py`

验证命令：
```powershell
python -B -m unittest tests.test_tiku_agent_fastapi_demo tests.test_tiku_agent_agent tests.test_tiku_agent_action_permissions_v2
node --check tiku_agent\demo_web\demo.js
python -B -m unittest discover -s tests -p "test_*.py"
```

### R5 真实矩阵两项误杀精修（Codex 复核修复）
复查严格白名单后的真实Qwen 62/70矩阵，确认8条兜底中有2条属于误杀：`WAIT_CHAPTER`中模型正确询问“属于哪个章节？”仅因问号被全局拒绝；`NO_MATCH`中模型先介绍一般工作流程再明确“当前无匹配，建议换章节或重发”，因宽泛候选正则误判为当前已有候选。

修复保持最小范围：只有`greeting + WAIT_CHAPTER + waiting_for=章节`、整句仅一个末尾问号且确实在询问所属章节时允许问号；其他类别、阶段、普通自问、多个问号继续拒绝。候选规则只把“请从/在/于候选中选择”或“请选择候选”等当前选择指令视为正面候选信号，不再把“请发题图→检索候选→选定答案”的一般流程说明当成当前候选。原112条护栏全部原样通过，新增测试同时锁定两条放行和原候选/问号拦截，相关54项与全量320项测试通过；路由、工具、状态与模型上下文均未修改。

改动文件：
- `tiku_agent/safe_answer_contract_v0.py`
- `tests/test_tiku_agent_safe_answer_contract_v0.py`

验证命令：
```powershell
python -B -m unittest tests.test_tiku_agent_safe_answer_contract_v0 tests.test_tiku_agent_safe_answer_state_consistency_v0 tests.test_tiku_agent_safe_answer_generator_v0 tests.test_tiku_agent_safe_answer_route_v0 tests.test_tiku_agent_safe_answer_state_aware
python -B -m unittest discover -s tests -p "test_*.py"
```

### R4 唯一候选的简短确认规则（体验修复）
当状态为`WAIT_CANDIDATE_CHOICE`、当前命名空间是候选且恰好只有1个候选时，用户整句回复“是/是的/对/没错/确认/确定/可以/行/好/好的”等简短肯定词，会由固定Intent V2规则直接解析为`select_candidate(rank=1)`，不进入安全回答模型，也不需要自主Planner。原有“就这个/就它/选这个”继续支持。规则使用整句精确匹配；多个候选、答案已经显示或包含其他意图时不会自动选择，等待章节时“可以”仍只接受已明确提供的全局搜索兜底。

新增意图边界测试和启用安全回答时的完整Agent链路测试，复现“系统询问是否选择唯一候选→用户回复‘是’→直接返回答案”；相关79项和全量318项测试通过。

改动文件：
- `tiku_agent/intent_v2.py`
- `tests/test_tiku_agent_intent_v2.py`
- `tests/test_tiku_agent_agent.py`

验证命令：
```powershell
python -B -m unittest tests.test_tiku_agent_intent_v2 tests.test_tiku_agent_agent tests.test_tiku_agent_safe_answer_route_v0
python -B -m unittest discover -s tests -p "test_*.py"
```

### R3 严格模型白名单与代码校验事实拆分（Codex 复核修复）
`SafeConversationContext`现在严格只含六项模型可见字段：`phase/chapter/candidate_count/allowed_actions/last_completed_step/waiting_for`。原先混在同一对象中的`question_count/has_active_image/has_answer/global_search_offered/continuation_available`不再进入模型上下文或`to_prompt_payload()`；动作权限仍直接从`AgentState`经权限矩阵计算，题图和答案存在性则进入独立的`SafeAnswerValidationFacts`，只供代码矛盾校验使用。Agent、generator、固定兜底和评估脚本已分别传递模型上下文与校验事实，业务状态机和工具未改。

7阶段逐项检查均只有相同六个白名单键；112条状态一致性护栏继续全部通过，相关78项与全量316项测试通过。严格白名单后的真实Qwen矩阵为62/70直接通过；等待题目选择阶段8/10直接结合阶段回答，另2条重新索要题图的输出被代码按`image_state_conflict`拦截，说明校验事实拆出后保护仍有效。评测产物位于忽略目录`.tmp_safe_answer_eval_8794/20260801_145756/`，不提交。

改动文件：
- `tiku_agent/safe_answer_context_v0.py`
- `tiku_agent/safe_answer_contract_v0.py`
- `tiku_agent/safe_answer_generator_v0.py`
- `tiku_agent/safe_answer_reply_v0.py`
- `tiku_agent/agent.py`
- `scripts/evaluate_safe_answer_qwen_v0.py`
- `tests/fixtures/safe_answer_state_consistency_v0_cases.json`
- `tests/test_tiku_agent_safe_answer_context_v0.py`
- `tests/test_tiku_agent_safe_answer_contract_v0.py`
- `tests/test_tiku_agent_safe_answer_generator_v0.py`
- `tests/test_tiku_agent_safe_answer_state_aware.py`
- `tests/test_tiku_agent_safe_answer_state_consistency_v0.py`

验证命令：
```powershell
python -B -m unittest tests.test_tiku_agent_safe_answer_context_v0 tests.test_tiku_agent_safe_answer_contract_v0 tests.test_tiku_agent_safe_answer_generator_v0 tests.test_tiku_agent_safe_answer_route_v0 tests.test_tiku_agent_safe_answer_state_aware tests.test_tiku_agent_safe_answer_state_consistency_v0 tests.test_tiku_agent_safe_answer_qwen_v0
python -B -m unittest discover -s tests -p "test_*.py"
$env:PYTHONIOENCODING='utf-8'; python -B scripts/evaluate_safe_answer_qwen_v0.py --matrix
```

### 已接受取舍：不增加阶段专用安全寒暄兜底
业务状态机已经在等待章节、候选展示、错误重试等节点给出完整业务提示，且安全回答失败不会改变或丢失业务状态。经讨论，暂不为每个寒暄类别×阶段铺设固定文案；模型失败时继续使用按类别的通用安全兜底，后续业务输入仍正常进入状态机。这是明确接受的轻微体验取舍，不再视为待修复缺口。

### R2 内部 action 中文翻译（Codex 复核修复）
业务权限矩阵仍以原始 action 判断合法性，但 `SafeConversationContext.allowed_actions` 不再保存或展示 `select_candidate`、`retry_search` 等执行器标识。新增11项完整固定映射，把所有可暴露动作翻译为“选择候选题”“查看下一批候选”“重试刚才的操作”等用户语言；`cancel`和`search_image`继续不进入安全回答面。上下文构造会拒绝未经审查的标签，测试保证映射键完整覆盖允许宇宙、prompt 与业务 action 集合不相交。

7阶段实际 prompt 抽样全部只显示中文，`RAW_ACTION_LEAKS=[]`；相关69项和全量311项测试通过，业务权限、路由、工具与状态均未改。

随后新增 `--compare-action-labels` 真实 Qwen 配对评测，只在评测请求中把“允许的下一步”切换为内部英文名或中文标签，生产上下文始终保持中文。7阶段×10话术×2组共140次调用，调用顺序交替以减小简单的先后偏差。结果：中文组直接通过64/70（91.4%），英文组63/70（90.0%）；只看含动作提示的60组，中文54/60（90.0%），英文53/60（88.3%）；阶段反映率中文47/60（78.3%），英文46/60（76.7%）；两组实际输出均未复述内部action。配对结果为61组都通过、4组都兜底、中文改善3组、中文退步2组。结论是翻译未造成明显效果下降，略有正向信号，但单轮随机模型差异很小，主要收益仍是降低内部实现词汇暴露风险，而不是显著提升回答能力。评测产物位于忽略目录 `.tmp_safe_answer_eval_8794/20260801_142227/`，不提交。

改动文件：
- `tiku_agent/safe_answer_context_v0.py`
- `tests/test_tiku_agent_safe_answer_context_v0.py`
- `tests/test_tiku_agent_safe_answer_contract_v0.py`
- `tests/test_tiku_agent_safe_answer_generator_v0.py`
- `tests/test_tiku_agent_safe_answer_state_aware.py`
- `scripts/evaluate_safe_answer_qwen_v0.py`（新增仅评测用的原始action/中文标签A/B开关）
- `tests/test_tiku_agent_safe_answer_qwen_v0.py`

验证命令：
```powershell
python -B -m unittest tests.test_tiku_agent_safe_answer_state_consistency_v0 tests.test_tiku_agent_safe_answer_context_v0 tests.test_tiku_agent_safe_answer_contract_v0 tests.test_tiku_agent_safe_answer_generator_v0 tests.test_tiku_agent_safe_answer_route_v0 tests.test_tiku_agent_safe_answer_state_aware
python -B -m unittest discover -s tests -p "test_*.py"
$env:PYTHONIOENCODING='utf-8'; python -B scripts/evaluate_safe_answer_qwen_v0.py --compare-action-labels
```

### R1 状态矛盾输出校验（Codex 复核修复）
真实 Qwen 矩阵确认 prompt 状态感知有效，但旧校验器只接收 `text + category`，会放行“等待章节时重新索要题图”“已有章节时再次询问章节”“ERROR 时编造章节判断失败”等矛盾回答。现已让输出校验器接收 `SafeConversationContext`，只拦截高确定性矛盾：内部 phase/action 标识、候选数量不一致、无候选/无答案时的正面声称、已有题图时重复索图、已有章节时声称未知或再次索要、ERROR 时编造具体错误原因。无 context 的 V0 调用保持兼容。

首次真实复测发现 `WAIT_QUESTION_CHOICE` 中“候选题目”被误当成检索候选，规则已收窄；第二次复测发现“无需提供章节”被误判成索要章节，已增加否定句豁免。随后新增112条离线护栏测试集（7阶段，56条应放行、56条应拒绝）：首跑找出13个近义词、中文数字、反向状态和具体错误原因漏口；修复后全量回归又发现2个身份/能力介绍误杀，最终收窄为只有当前结果信号或明确数量冲突才拒绝。护栏112/112、相关53项与全量311项测试通过。本步没有修改路由、工具或 `AgentState`。

改动文件：
- `tiku_agent/safe_answer_contract_v0.py`
- `tiku_agent/safe_answer_generator_v0.py`
- `tiku_agent/safe_answer_reply_v0.py`
- `tests/test_tiku_agent_safe_answer_contract_v0.py`
- `tests/fixtures/safe_answer_state_consistency_v0_cases.json`
- `tests/test_tiku_agent_safe_answer_state_consistency_v0.py`

验证命令：
```powershell
python -B -m unittest tests.test_tiku_agent_safe_answer_contract_v0 tests.test_tiku_agent_safe_answer_generator_v0 tests.test_tiku_agent_safe_answer_route_v0 tests.test_tiku_agent_safe_answer_state_aware
python -B -m unittest tests.test_tiku_agent_safe_answer_state_consistency_v0 -v
python -B -m unittest discover -s tests -p "test_*.py"
$env:PYTHONIOENCODING='utf-8'; python -B scripts/evaluate_safe_answer_qwen_v0.py --matrix
```

### M6 离线状态感知矩阵评估 + 寒暄反射指令强化
`scripts/evaluate_safe_answer_qwen_v0.py` 新增 `--matrix` 模式：7 阶段 × 10 话术 = 70 组合，构造合法 `AgentState` → `build_safe_answer_context` → 真实 Qwen `generate(text, context)`。首轮发现缺口：内容性话术（致谢/身份/能力/流程）状态感知强，但**纯寒暄（你好/在吗）默认回"请发送题图"**，状态段被当可选参考。在 `safe_answer_contract_v0.py` 新增 `SAFE_ANSWER_STATE_REFLECT_V0`：按「等待」字段显式映射（等待章节→请告知章节；等待题目选择→请选题目；候选就绪→提及数量请选；无匹配→提示换章节/新题图；出错→提示重试；答案返回→提及可查看），并禁止"已找到/已检索"类完成时声称。三版迭代后：**70/70 契约通过**（第三版 66/70 accepted，4 条仅因模型自问句带问号被 `unsolicited_question` 拦截回落，属安全兜底正常工作），各阶段寒暄全部状态感知。契约测试补反射指令断言（有 context 在、IDLE 不在）。

### M5 runtime 级验收测试
新增 `tests/test_tiku_agent_safe_answer_state_aware.py`，把 `TikuSearchAgent` 当黑盒：构造 state → 发寒暄/业务句 → 断言模型收到的 prompt、用户回复、状态是否变更、业务工具是否被调。11 条测试覆盖：全 11 阶段寒暄 prompt 含 phase 标识、敏感字段不泄露（session_id/路径/.jpg/score/stack）、候选寒暄含候选数量、章节致谢含 global_search、答案后不泄露路径、8 种业务句不误入、模型超时/报错/输出违规回落通用兜底、allowed_actions 与权限矩阵一致、跨重启（to_dict/from_dict 重建后仍状态感知）、跨用户隔离。全部注入假 model / 构造假 state，不连真实模型与题库。

### M3 接线后按需兜底文案（空表机制）
`render_safe_answer_v0` 增加 `_PHASE_REPLY_BUILDERS[(category, phase)]` 查询：命中 builder 则用过输出契约的 phase 文案；否则回落 `_SAFE_REPLIES` 通用兜底。表**有意保持为空**——各阶段业务引导已由 render.py 状态机承担（章节提示/全局搜索/错误重试/候选列表），纯寒暄兜底不需要 phase 专用文案；模型失败时永远回落合法单行回答，不会无回答。新增 3 条边界测试：空表逐 phase 回落、命中仅覆盖本 (category, phase)、builder 违反契约回落。

### M1 状态摘要模块（commit `82634dd`）
新增纯模块 `safe_answer_context_v0.py`，从 `AgentState` 派生脱敏白名单摘要 `SafeConversationContext`。allowed_actions 动态权限矩阵派生；phase 英文常量、waiting_for/last_completed_step 中文；路径/分数/错误/session_id 不入白名单；IDLE 无状态小节。18 个单测 + 全量通过。

---

## 步骤：M6 离线状态感知矩阵评估 + 寒暄反射指令强化

### 干了啥
1. **矩阵评估**：`scripts/evaluate_safe_answer_qwen_v0.py` 新增 `--matrix` 模式，7 阶段（IDLE/WAIT_CHAPTER/WAIT_QUESTION_CHOICE/WAIT_CANDIDATE_CHOICE/ANSWERED/NO_MATCH/ERROR）× 10 话术 = 70 组合，每组合构造合法 `AgentState` → `build_safe_answer_context` → 真实 Qwen `generate(text, context)`。输出 `matrix_report.txt`（每阶段每话术的模型真实回答）+ `records.jsonl` + `summary.json`。
2. **发现缺口**：内容性话术（致谢/身份/能力/流程）状态感知强；但**纯寒暄（你好/在吗）默认回"请发送题图"**——状态段在 prompt 里存在，但 `SAFE_ANSWER_STATE_GUARD_V0` 把它定为"只用于组织措辞"（可选参考），模型寒暄时走通用开场。
3. **修复**：`safe_answer_contract_v0.py` 新增 `SAFE_ANSWER_STATE_REFLECT_V0`，有状态段时（非 IDLE）插入。按「等待」字段显式映射：等待章节→请告知章节；等待题目选择→请选题目；候选就绪→提及候选数量请选；无匹配→提示换章节/新题图；出错→提示重试/新题图；答案已返回→提及可查看。并明确"不要用'已找到、已检索、已查到、已读取'等完成时声称已执行检索"（治掉首版 WAIT_CANDIDATE_CHOICE 用"已找到"被 `fabricated_execution_claim` 拦截的问题）。
4. **迭代**：三版。V1（无反射指令）21/21 greeting 通过但寒暄不感知状态；V2（通用一句话反射指令）greeting 部分感知但 2 条踩执行声称/问号拦截；V3（按等待项显式映射）全阶段寒暄感知，66/70 accepted，回落全部为 `output_unsolicited_question`（模型自问句带问号），无执行声称问题。

### 改动文件
- `scripts/evaluate_safe_answer_qwen_v0.py` → 新增 `--matrix`、`MATRIX_PHASES`/`MATRIX_UTTERANCES`/`build_phase_state`/`evaluate_matrix`/`render_matrix_report`/`run_matrix_evaluation`；import `build_safe_answer_context`、AgentState 与阶段常量。
- `tiku_agent/safe_answer_contract_v0.py` → 新增 `SAFE_ANSWER_STATE_REFLECT_V0` 常量；`build_safe_answer_prompt_v0` 有状态段时在其后插入反射指令。
- `tests/test_tiku_agent_safe_answer_contract_v0.py` → 有 context 时断言反射指令存在、IDLE 时断言不存在。

### 验证命令与结果
```
python -B -m unittest tests.test_tiku_agent_safe_answer_contract_v0 tests.test_tiku_agent_safe_answer_context_v0 tests.test_tiku_agent_safe_answer_state_aware   # 42/42 通过
PYTHONIOENCODING=utf-8 python -B scripts/evaluate_safe_answer_qwen_v0.py --matrix   # 70/70 契约通过（第三版 66 accepted + 4 条问号拦截回落）；结果在 .tmp_safe_answer_eval_8794/
```

### 审查关注点
- `SAFE_ANSWER_STATE_REFLECT_V0` 只在非 IDLE（有状态段）时插入；IDLE 保持 guard-only。
- 反射指令是 prompt 层指引，不含被禁执行动词的**输出**，不会误伤 `validate_safe_answer_output_v0`（校验的是模型输出，prompt 内禁词列举是给模型看的指引）。
- 反射指令里的完成时动词列举与校验器 `_EXECUTION_CLAIM_PATTERN` 一致（检索/找到/查到/读取），未列入"已返回"（状态段 `答案已返回` 是合法文案，非禁词）。
- `--matrix` 用真实 Qwen 调用，每次约 1.5s，70 次 ≈ 2 分钟；需 `DASHSCOPE_API_KEY` 环境变量。
- 4 条 `output_unsolicited_question` 回落是安全边界内正常拦截（prompt 已要求"不主动追问"，个别情况模型仍带问号），非状态感知失败。

### 剩余任务 / 已知风险
- 无。M6 为收官步骤，三版迭代已收敛：全阶段寒暄状态感知 + 70/70 契约通过 + 无执行声称泄漏。
- 已知用户决策：`_PHASE_REPLY_BUILDERS` 保持为空（M3 结论）；业务层 render.py 措辞不改；`SAFE_ANSWER_STATE_REFLECT_V0` 迭代到此定版，不再继续调问号拦截率。

### 建议下一步命令
- 8793 评审：读本交接文档 → 按 M1→M6 顺序逐个 `git show <commit>` + 跑上述验证命令。
- 试跑 8794 服务：`python -B scripts/run_tiku_agent_8794.py --port 8794`（默认启用安全回答，题库在 `config.local.json` 的 `root`）。

---

## 步骤：M5 runtime 级验收测试

### 干了啥
把 `TikuSearchAgent` 当黑盒验收：构造合法 `AgentState` → 发寒暄/业务句 → 断言模型收到的 prompt、用户回复、状态是否变更、业务工具是否被调。全部注入假 model（capturing `SafeAnswerGeneratorV0`）、构造假 state，不连真实模型与题库。11 条测试覆盖 roadmap Stage 4 全部验收点：
- 全 11 阶段寒暄均到达模型，prompt 含 `阶段：{phase}`（IDLE 无状态小节、不声称阶段）；
- 任何 phase 的 prompt 不泄露敏感字段（session_id/路径/.jpg/score/stack）；
- 候选阶段寒暄含候选数量与「等待：候选选择」；
- 等待章节致谢含全局搜索提示（global_search_offered）；
- 答案后寒暄不泄露答案路径（answer.png）；
- 8 种业务句（选1/继续搜/换第五章/全局搜索/取消/重发答案/第2题/把答案发给我）不误入安全回答、不调 generator；
- 模型超时/报错/输出违规（4 类）回落通用固定兜底且单行≤90 字；
- allowed_actions 与权限矩阵一致（WAIT_CANDIDATE_CHOICE/ANSWERED/NO_MATCH）；
- 跨重启：`state.to_dict()` → `AgentState.from_dict` 重建后仍状态感知；
- 跨用户：user-a/user-b 会话各自隔离，prompt 不含对方信息。

### 改动文件
- `tests/test_tiku_agent_safe_answer_state_aware.py` → 新增（仅此一个文件，本轮零生产代码改动）。

### 验证命令与结果
```
python -B -m unittest tests.test_tiku_agent_safe_answer_state_aware -v   # 11/11 通过
python -B -m unittest discover -s tests -p "test_*.py"                   # 306 全量 OK，无回归
```

### 审查关注点
- 是否真零工具调用：`_toolbox_that_must_not_run` 的 mock 一旦被调即断言失败。
- 业务句不误入：`test_business_text_never_enters_safe_answer_in_candidate_phase` 用 Mock generator 断言 generate 未被调。
- 状态零改动：每个用例前后 `state.to_dict()` 相等。
- prompt 脱敏：`_assert_sensitive_fields_absent` 同时覆盖 system_prompt 与 user_prompt。
- 跨重启/跨用户为真重建（from_dict）与真独立实例，非共享状态。

### 剩余任务 / 已知风险
- 无。M1–M5 全部完成，状态感知安全回答（roadmap Stage 4）验收通过。
- 可交 8793 评审后再提升 8790；离线状态感知评估（`scripts/evaluate_safe_answer_qwen_v0.py` 现以 context=None 调用）留作后续独立小步。

### 建议下一步命令
- 8793 评审：读本交接文档 → 按 M1→M5 顺序逐个 `git show <commit>` + 跑上述验证命令。

---

## 步骤：M3 接线后按需兜底文案（空表机制）

### 干了啥
`render_safe_answer_v0(category, context=None)` 增加 `_PHASE_REPLY_BUILDERS[(category, phase)]` 查询：命中 builder 则用其文案（必须过 `validate_safe_answer_output_v0`，不过回落）；否则回落 `_SAFE_REPLIES` 通用兜底。表**有意保持为空**——各阶段业务引导已由 render.py 状态机承担（`render_chapter_prompt`/`render_error`/`render_candidates` 等），纯寒暄兜底不需要 phase 专用文案。模型失败时永远回落合法单行回答，不会无回答。

### 改动文件
- `tiku_agent/safe_answer_reply_v0.py` → 新增 `_PHASE_REPLY_BUILDERS`（空 dict）、`render_safe_answer_v0` 由 `del context` 改为查询 builder、import `Callable`。
- `tests/test_tiku_agent_safe_answer_contract_v0.py` → 新增 `test_empty_phase_builder_table_falls_back_to_generic_for_every_phase`、`test_registered_phase_builder_overrides_only_its_own_phase`、`test_phase_builder_violating_the_contract_falls_back_to_generic`。

### 验证命令与结果
```
python -B -m unittest tests.test_tiku_agent_safe_answer_contract_v0 tests.test_tiku_agent_safe_answer_generator_v0 tests.test_tiku_agent_safe_answer_route_v0 tests.test_tiku_agent_safe_answer_context_v0   # 53/53 通过
python -B -m unittest discover -s tests -p "test_*.py"                                                                                                                                                # 全量 OK，无回归
```

### 审查关注点
- `_PHASE_REPLY_BUILDERS` 是否确实为空（生产行为 = 通用兜底，与 M4 之前逐字节一致）。
- 命中 builder 是否只覆盖本 (category, phase)（其他 phase/类别回落通用）。
- builder 输出违反契约时是否回落通用而非返回违规文案。
- 业务话术（章节/全局搜索/重试/候选引导）是否未混入兜底表——表空即证明。

### 剩余任务 / 已知风险
- 无。表空是**有意设计**（用户拍板），不是未完成项；将来某阶段确实需要 phase 文案时直接注册 builder 即可。

### 建议下一步命令
- M5 实施前审查本步：`git show <m3-commit>`；运行上述验证命令。

---

## 步骤：M4 generator 透传 + agent 接线

### 干了啥
让状态感知真正进入安全回答主路径：
- `safe_answer_reply_v0.py` 的 `render_safe_answer_v0` 新增可选 `context: SafeConversationContext | None = None` 参数（`del context`，暂不感知，M3 填查询表；无 context 调用行为不变）。
- `safe_answer_generator_v0.py` 的 `generate(user_text, context=None)` 把 context 传给 `build_safe_answer_prompt_v0`；`_fallback(category, reason, started, context=None)` 把 context 传给 `render_safe_answer_v0`。无 context 时行为不变。
- `agent.py` 的 `_safe_answer_response` 先构建 `context = self._safe_answer_context()`，传给 `generate` 与兜底 `render_safe_answer_v0`。
- 新增 `agent._safe_answer_context()`：`try: return build_safe_answer_context(self.state) except Exception: return None`——状态摘要派生失败时降级为 state-free，绝不破坏安全回答。
- `handle_text`、general 保险网、`_stop_for_tool_result`、`_dispatch` 全部未动。

### 改动文件
- `tiku_agent/safe_answer_reply_v0.py` → `render_safe_answer_v0` 加 `context` 参数（del，行为不变）。
- `tiku_agent/safe_answer_generator_v0.py` → `generate`/`_fallback` 加 `context` 参数并透传。
- `tiku_agent/agent.py` → `_safe_answer_response` 接线；新增 `_safe_answer_context()`；新增 import `SafeConversationContext`/`build_safe_answer_context`。
- `tests/test_tiku_agent_safe_answer_generator_v0.py` → 新增 `test_generate_passes_whitelisted_context_into_the_prompt`、`test_generate_without_context_remains_state_free`、`test_generate_fallback_with_context_passes_context_to_render`。
- `tests/test_tiku_agent_safe_answer_route_v0.py` → 新增 `test_candidate_phase_greeting_is_phase_aware_zero_tool_zero_state`、`test_context_derivation_failure_degrades_to_state_free_fallback`。

### 验证命令与结果
```
python -B -m unittest tests.test_tiku_agent_safe_answer_generator_v0 tests.test_tiku_agent_safe_answer_route_v0   # 22/22 通过
python -B -m unittest discover -s tests -p "test_*.py"                                                          # 292 全量 OK，无回归
```

### 审查关注点
- 无 context 调用路径是否保持原有行为（generator/agent 既有 38 条 route + 全量契约测试原样通过）。
- `_safe_answer_context()` 异常降级是否真能触发（`test_context_derivation_failure_degrades_to_state_free_fallback` 用 patch 强制 `build_safe_answer_context` 抛错，确认回落固定兜底且不调工具）。
- 注入 generator 是否收到白名单 context（`test_candidate_phase_greeting_is_phase_aware_zero_tool_zero_state` 断言 prompt 含 `WAIT_CANDIDATE_CHOICE`/`候选数量：3`，且状态零改动、零工具调用）。
- `render_safe_answer_v0` 加了 `context` 参数但当前 `del context`，M3 之前无 phase 感知文案属预期。

### 剩余任务 / 已知风险
- 无已知风险。M3 将在本步之后实施（接线完再看哪些兜底真需要 phase 感知文案）。
- 已知用户决策：业务回答与安全回答保持分离，业务话术（章节/全局搜索/重试/候选引导）不进入安全兜底，业务层 `render.py` 措辞不改。

### 建议下一步命令
- M3 实施前审查本步：`git show <m4-commit>`；运行上述两个验证命令。

---

## 步骤：M2 prompt 契约扩展

### 干了啥
`build_safe_answer_prompt_v0` 新增可选 `context: SafeConversationContext | None = None` 参数。有 context 时在 style 与类别指引之间插入 `SAFE_ANSWER_STATE_GUARD_V0`（防逐字复述/防编造执行）+ `render_state_section(context)`（IDLE 为空则跳过）；无 context 时 system_prompt 与原来逐字节相同。`validate_safe_answer_output_v0` 未改。

### 改动文件
- `tiku_agent/safe_answer_contract_v0.py` → `build_safe_answer_prompt_v0` 加 context 参数、新增 `SAFE_ANSWER_STATE_GUARD_V0` 常量、import `SafeConversationContext`/`render_state_section`。
- `tests/test_tiku_agent_safe_answer_contract_v0.py` → 新增 `test_prompt_with_context_contains_only_whitelisted_fields`、`test_prompt_without_context_remains_state_free`、`test_prompt_with_idle_context_skips_state_section`。

### 验证命令与结果
```
python -B -m unittest tests.test_tiku_agent_safe_answer_contract_v0 tests.test_tiku_agent_safe_answer_context_v0   # 28/28 通过
python -B -m unittest discover -s tests -p "test_*.py"                                                              # 全量 OK，无回归
```

### 审查关注点
- 无 context 调用路径是否逐字节不变（原 38 条 state-free 契约测试应原样通过）。
- 有 context 的 prompt 是否只含白名单字段（不含 session_id/路径/`.jpg`/`.xlsx`/score/stack）。
- `validate_safe_answer_output_v0` 是否未被削弱。
- IDLE context 是否只保留 guard、不插入空状态小节。

### 剩余任务 / 已知风险
- 无已知风险。M2 仍无调用方（generator 尚未接线，M4 接入）。

### 建议下一步命令
- M3 实施前审查本步：`git show <m2-commit>`；运行上述两个验证命令。

---

## 步骤：Stage 5 影子规划 + 权限审核（S1–S5 分步落地）

### 干了啥
在 8794 候选上实现 roadmap Stage 5 的第一段：一个 AI Planner 对固定状态机处理不了的长尾请求提出结构化计划（目标/步骤/参数/停止条件），由代码权限层审核（只读/phase/题目版本/候选批次/章节/参数范围/预算），**只生成、审核、记录，不执行工具、不修改业务状态、不改变用户最终回答**。

分五步落地：
- **S1 纯数据结构模块 `tiku_agent/shadow_plan_v0.py`**：`ShadowPlan`/`ShadowPlanStep`/`PermissionReview`/`PermissionReviewFacts` 四个 frozen dataclass + `review_shadow_plan(plan, facts)` 纯审核函数 + `build_permission_review_facts(state)` 代码侧事实派生。计划动作宇宙 = `TASK_ACTIONS - {cancel, search_image}`；预算常量 `MAX_PLAN_STEPS=4`、`MAX_PLANS_PER_TURN=1`。step 合法性复用现有 `authorize_action_v2` 权限矩阵；版本/批次校验复刻 `fastapi_demo._validate_action_context` 逻辑（这是 agent 侧首次检查 `task_revision`/`candidate_generation` 一致性）。
- **S2 规划器 prompt + 客户端 `tiku_agent/shadow_planner_v0.py`**：`SHADOW_PLAN_PROMPT`（只读规划器角色 + 动作宇宙 + 参数要求 + 禁写/禁编造）、`build_shadow_plan_prompt_v0`（用户原话 + `ConversationContextV2.to_prompt_payload()` 脱敏摘要）、`parse_shadow_plan_v0` 严格解析、`ShadowPlannerV0`（注入 model_client，任何失败降级 `None`）、`call_qwen_planner_v0`（环境变量读 key）。规划器模型只拿脱敏摘要，**从不接触 `AgentState`**。
- **S3 agent 接线 `tiku_agent/agent.py`**：`__init__` 加 `shadow_planner`/`shadow_logger` 可选参数（默认 None=完全关闭，行为逐字节一致）；`handle_text` 两个 `decide_intent_v2` 后插 `_maybe_shadow_plan`；触发 = `clarification` 且 reason ∈ `{ambiguous_reference, ambiguous_action, ambiguous_number_namespace}`（`missing_*`/`out_of_range` 等固定回复不烧模型调用）；单回合最多一次（入口重置 + 防御计数）；整段 `try/except Exception: pass`。
- **S4 影子日志 `tiku_agent/shadow_plan_log.py`**：`ShadowPlanLogEntry`（含 `user_text` 评审用、`plan`、`review`、`trigger_reason`、`phase_before`、`planner_unavailable`）+ `JsonlShadowPlanLogger`（JSONL 追加，`session_key` 哈希与 task_log 一致）。只写 8794 运行目录 `shadow_plans.jsonl`，不入 git。
- **S5 runner 接线 `scripts/run_tiku_agent_8794.py`**：`build_runtime`/`build_app` 加 `enable_shadow_planning`（默认 True，注入真实 Qwen 规划器 + 日志），argparse 互斥组 `--enable-shadow-planning`/`--disable-shadow-planning`。

### 改动文件
- `tiku_agent/shadow_plan_v0.py`（新增）
- `tiku_agent/shadow_planner_v0.py`（新增）
- `tiku_agent/shadow_plan_log.py`（新增）
- `tiku_agent/agent.py`（修改）
- `scripts/run_tiku_agent_8794.py`（修改）
- `tests/test_tiku_agent_shadow_plan_v0.py`（新增，18 条纯函数）
- `tests/test_tiku_agent_shadow_planner_v0.py`（新增，15 条 prompt/解析/客户端）
- `tests/test_tiku_agent_agent.py`（追加 5 条接线测试）

### 验证命令与结果
```powershell
python -B -m unittest tests.test_tiku_agent_shadow_plan_v0 tests.test_tiku_agent_shadow_planner_v0 tests.test_tiku_agent_agent   # 33+45 全过
python -B -m unittest discover -s tests -p "test_*.py"   # 361 全量通过，无回归
```

### 审查关注点
- **零影响主路径**：`shadow_planner=None` 时行为逐字节不变；接线测试证明开/关影子规划同一请求 `AgentResponse` 的 text 与业务 state 一致。
- **架构边界**：规划器模型只收 `ConversationContextV2.to_prompt_payload()`，prompt 测试断言不含 session_id/路径/score/stack；执行权始终在代码（step 走现有权限矩阵）。
- **触发收敛**：只有 `ambiguous_*` 长尾进规划器，`missing_*`/`out_of_range` 等固定回复不触发（0 模型调用）。
- **越权必拒**：写动作、旧版本、旧批次、非法章节、越界参数、超预算均 reject 且记中文原因。
- **单回合预算**：最多一次规划（计数 + `MAX_PLANS_PER_TURN`）。
- **失败隔离**：规划器抛错/坏 JSON/超时一律 `None` 或吞掉，绝不进用户回复路径。

### 剩余任务 / 已知风险
- **8793 无法自动观测 8794 影子计划**：8793 镜像 8790 主线，不跑 8794 代码。影子计划目前只能人工读 8794 运行目录 `shadow_plans.jsonl`（含用户原话，评审数据，不入 git）。"自动问题交接闭环"仍挂起，接受为本阶段限制。
- **影子规划是同步模型调用**：长尾请求会多一次 Qwen 调用（约 1.5s+），用户回复内容不变但延迟增加。这是 Stage 5 记录的本质，后续如需可改成异步。
- **测试用注入 stub，未连真实 Qwen 跑影子规划矩阵**：真实长尾观察待 8794 服务人工验证（建议下一步：浏览器发含糊请求，确认 shadow_plans.jsonl 追加）。
- 未提升 8790；8790/8793/8788 未受影响。

### 建议下一步命令
```powershell
# 1) 起 8794 服务（默认启用影子规划）
python -B scripts/run_tiku_agent_8794.py --port 8794
# 2) 浏览器 http://127.0.0.1:8794 发一句含糊请求（如"这个题你帮我看看"）
# 3) 确认 .tmp_tiku_agent_v2_candidate_8794\shadow_plans.jsonl 追加了 plan+review
# 4) 交 Codex 审查：读本交接文档 → git diff 改动文件清单 → 跑上述验证命令
```

---

## 步骤：Stage 5 真实 Qwen 矩阵评估（发现审核事实缺口）

### 干了啥
按 Stage 4 M6 的成熟套路，为影子规划写真实效果评估脚本 `scripts/evaluate_shadow_plan_qwen_v0.py`：
- 7 阶段（IDLE/WAIT_CHAPTER/WAIT_QUESTION_CHOICE/WAIT_CANDIDATE_CHOICE/ANSWERED/NO_MATCH/ERROR）× 15 条**真实长尾口语话术**（"这个题你帮我看看""那换一道难一点的吧""它怎么说的来着"等含糊指代/省略/换目标，非 fixture 书面例句）= 105 格。
- 每格：构造合法 `AgentState` → `build_runtime_context_v2(...).to_prompt_payload()` 脱敏摘要 → 真实 Qwen 规划器 `ShadowPlannerV0.plan()` → `review_shadow_plan` → 记录 plan+review。
- 输出 `.tmp_shadow_plan_eval_8794/<时间戳>/`（records.jsonl + summary.json + matrix_report.txt），忽略目录不提交。

### 结果
- **结构合法率 105/105 = 100%**：真实 Qwen 规划器每条都能产出可解析的 `ShadowPlan`——prompt 契约 + 严格解析工作正常。
- 总 review 通过率 41/105 = 39%。分阶段：
  - WAIT_QUESTION_CHOICE 10/15、WAIT_CANDIDATE_CHOICE 14/15、ANSWERED 14/15——**通过率高且计划质量好**（正确索引 select_question、reject_candidates+continue_search、report_answer_mismatch、resend_answer 等贴合用户意图）。
  - IDLE / NO_MATCH / ERROR **0/15 全拒**。

### 发现的真实缺口（注入测试发现不了的）
拒绝码分布：`explainable_failure_required` **55 次**占大头。逐格核对确认**不是模型错，是审核事实缺失**：
- NO_MATCH 用户问"它怎么说的来着" → 规划器合理提议 `explain_failure()` → 被拒。
- ERROR 用户问"为啥失败" → 规划器合理提议 `explain_failure()` → 被拒。
- 根因：`PermissionReviewFacts` **漏了 `has_explainable_failure` 和 `retryable_error` 两个字段**，`_facts_to_decision_context` 里二者硬编码为 False，导致 `explain_failure`/`retry_search` 这两个合法只读动作在任何阶段都被误杀。

其余拒绝码合理（非 bug）：`current_question_required`/`chapter_unknown_question_required`（IDLE/ANSWERED 提 global_search/set_chapter，阶段不合法）；`candidate_list_required`（WAIT_QUESTION_CHOICE 提 show_candidates，该阶段无候选）。

### 改动文件
- `scripts/evaluate_shadow_plan_qwen_v0.py`（新增，离线评估工具）

### 验证命令与结果
```powershell
PYTHONIOENCODING=utf-8 python -B scripts/evaluate_shadow_plan_qwen_v0.py   # 105 格，结构合法率 100%
python -B -m unittest discover -s tests -p "test_*.py"   # 361 全量通过，无回归
```

### 审查关注点
- 评估脚本是**纯离线工具**，不接线到任何生产路径；全部输出进忽略目录。
- 用 `DASHSCOPE_API_KEY` 环境变量，不落盘。
- 脚本自身用 stub 规划器冒烟测试过（105 格全跑通、统计正确），确保脚本无 bug 后再连真模型。

### 剩余任务 / 下一步
- **待修复**：`build_permission_review_facts` 补派生 `has_explainable_failure`（=`bool(state.last_error)`）和 `retryable_error`（=`state.phase==PHASE_ERROR and bool(state.active_image_path)`），并对齐 `safe_answer_context_v0._authorized_user_actions` 的写法；`_facts_to_decision_context` 透传。修完重跑矩阵对比通过率。
- 修复完成后补测试锁定：NO_MATCH 阶段 explain_failure 计划应放行、ERROR 阶段 retry_search 计划应放行。
- 后续：把修复后的矩阵结果与交接文档一并交 Codex 审查。

### 修复：PermissionReviewFacts 补 has_explainable_failure / retryable_error
真实矩阵暴露 `explainable_failure_required` 55 次误杀后，`shadow_plan_v0.py` 补齐审核事实并透传：
- `PermissionReviewFacts` 新增 `has_explainable_failure`、`retryable_error` 两个字段。
- `build_permission_review_facts` 派生：`has_explainable_failure = bool(state.last_error)`；`retryable_error = state.phase == PHASE_ERROR and bool(state.active_image_path)`（对齐 `safe_answer_context_v0._authorized_user_actions` 的既有写法，且 `retryable_error` 同时要求有活跃题图）。
- `_facts_to_decision_context` 透传两个字段给现有 `DecisionContextV2`。

新增 3 条测试锁定：NO_MATCH 阶段 `explain_failure` 计划放行；ERROR 阶段 `retry_search` 计划放行；ERROR 但**无活跃题图**时 `retry_search` 仍拒绝（`retryable_error` 的双条件边界）。相关测试 21 条、全量 364 条通过。

改动文件：
- `tiku_agent/shadow_plan_v0.py`
- `tests/test_tiku_agent_shadow_plan_v0.py`

验证命令：
```powershell
python -B -m unittest tests.test_tiku_agent_shadow_plan_v0   # 21/21 通过
python -B -m unittest discover -s tests -p "test_*.py"        # 364 全量通过
PYTHONIOENCODING=utf-8 python -B scripts/evaluate_shadow_plan_qwen_v0.py   # 重跑矩阵对比通过率
```

### 修复后真实矩阵重跑结果
修复 `PermissionReviewFacts` 缺字段后重跑同一矩阵（`.tmp_shadow_plan_eval_8794/20260801_182658/`）：

| 阶段 | 修复前 | 修复后 |
|---|---|---|
| 总通过率 | 41/105 (39.1%) | **59/105 (56.2%)** |
| WAIT_QUESTION_CHOICE | 10/15 | 8/15 |
| WAIT_CANDIDATE_CHOICE | 14/15 | **15/15** |
| ANSWERED | 14/15 | 14/15 |
| NO_MATCH | 0/15 | **9/15** |
| ERROR | 0/15 | **10/15** |
| IDLE | 0/15 | 0/15 |
| WAIT_CHAPTER | 3/15 | 3/15 |

- `explainable_failure_required` 从 55 降至 27——缺字段误杀已修复，NO_MATCH/ERROR 的 explain_failure/retry_search 计划正常放行。
- **结构合法率保持 100%**：规划器 prompt 契约稳定。

### 剩余两个待解问题（均已定位，未改代码）
1. **WAIT_CHAPTER 阶段规划器动作选择偏差**：等章节时用户说"有没有更接近的/后面那几道也看看/它怎么说的来着"，规划器大量提 `explain_failure()`，而非 `continue_search`/`show_candidates`。这不是审核误杀（无失败可解释时拒得对），是 `SHADOW_PLAN_PROMPT` 对动作语义的引导不足——需要更明确告诉模型"explain_failure 只在有失败可解释时用；续搜/看候选是常见诉求"。
2. **审核把 clarify 当 reject（语义粗粒度）**：IDLE 阶段规划器提 `set_chapter` 被拒为 `current_question_required`——该拒绝码在权限矩阵里实为 `clarify`（"缺信息可引导"），影子审核统一记为 reject。本阶段纯记录不执行，不会造成危险，只是观测分类不够精确；是否细化为记录取舍，留待 Codex 审查决定。

### 下一步候选
- 迭代 `SHADOW_PLAN_PROMPT` 修正 WAIT_CHAPTER 动作选择（对照 Stage 4 反射指令三版迭代的成熟做法）。
- 或在交接后交 Codex 审查当前结果，再决定是否继续调。

### 修复：预算下沉到工具级（Stage 6 工具硬上限预演）
Codex 审查意见（用户转达）指出：规划器的一步动作在真实状态机里展开成多个工具调用，影子规划的 `MAX_PLAN_STEPS=4` 是**动作级**预算，与 Stage 6"工具总数硬上限"（**工具级**）语义错位——一个 4 步 `select_question` 计划实际触发 4×4=16 次工具调用，预算失效。

审查核实的动作→工具展开（agent.py dispatch 链）：
- `retry_search`=7（复用已存图重跑全链）
- `set_chapter`/`select_question`=4（route→classify→coarse→rerank）
- `global_search`=3、`continue_search`=2、`select_candidate`=1、其余状态类动作=0

修复（`shadow_plan_v0.py`）：
- 新增 `ACTION_TOOL_COST` 常量：动作→保守工具数上界，注释显式指向 agent.py dispatch 链，改链必须同步。
- 新增 `MAX_TOOLS_PER_PLAN=8` 与 `PermissionReviewFacts.max_tools`。
- `review_shadow_plan` 在步数检查后新增工具预算检查：累加各 step 工具数超上限 → `plan_tools_budget_exceeded`。
- `_plan_tool_cost` 对未知动作按 0 计（不误伤），由测试强制补全映射。

新增 4 条测试：4 步 select_question（16 工具）被拒、continue_search+select_candidate（3 工具）放行、映射覆盖全部 PLAN_ACTION_UNIVERSE、映射值非负。相关测试 25 条、全量 368 条通过。

改动文件：
- `tiku_agent/shadow_plan_v0.py`
- `tests/test_tiku_agent_shadow_plan_v0.py`

验证命令：
```powershell
python -B -m unittest tests.test_tiku_agent_shadow_plan_v0   # 25/25
python -B -m unittest discover -s tests -p "test_*.py"        # 368 全量
PYTHONIOENCODING=utf-8 python -B scripts/evaluate_shadow_plan_qwen_v0.py   # 重跑矩阵对比
```

### 工具预算修复后真实矩阵重跑（`.tmp_shadow_plan_eval_8794/20260801_184352/`）
- 总通过率 **59/105 (56.19%)**，与修复前持平；结构合法率 100%。
- **`plan_tools_budget_exceeded` 未出现**：105 个真实计划工具成本分布 {0:71, 1:5, 2:2, 3:14, 4:11, 7:2}，**0 个超 8 工具预算**。结论：真实规划器天然不提议重计划，工具预算是**对抗性防御**（测试锁定：模型若提出 16 工具计划会被拦），对真实输出零误拦、零副作用。
- 核验脚本确认：无"超预算却 allow"的漏放行。

### 剩余待解问题（沿用上轮，未改）
1. **WAIT_CHAPTER 规划器动作选择偏差**（`explainable_failure_required` 仍 25 次）：规划器把"有没有更接近的/后面那几道也看看"误判为 explain_failure，应提 continue_search/show_candidates。属 prompt 引导不足，非审核 bug。
2. **审核把 clarify 当 reject**：缺信息可引导的动作被统一记为拒绝。纯记录阶段无危险，是否细化留 Codex 决定。

### 决策：WAIT_CHAPTER 规划器误判的处理方向（先审后改）
**现象**：WAIT_CHAPTER 15 条话术里 10 条被拒，`explainable_failure_required` 25 次。逐格核对模型的 step.reason 发现：**模型分析是对的**（它知道"应引导用户选章节/做全局搜索"），但**选错了动作名**——它想表达"我无法直接执行，需要解释/引导"，却把 `explain_failure` 当成了万能兜底。

**根因**：`explain_failure` 业务语义是"解释上一次失败的原因"（合法条件 `has_explainable_failure`=有 last_error），不是"解释我为什么做不到"。模型没有动作语义边界，拿不准时硬塞 `explain_failure`。

**决策：不改宇宙、不加 clarification。**
- `clarification` 是对话层动作（CONVERSATION_ACTIONS），不是"可执行计划"。放进计划宇宙会让执行器（Stage 6）拿到一个不知该执行什么的步骤，语义混乱。
- 保持"计划=可执行只读动作"的语义纯净。

**改法（双管）**：
1. **prompt 动作语义强化**：给每个动作边界定义。`explain_failure` 明确"只在确有失败可解释时用（错误/无匹配后询问原因）"；`continue_search`/`show_candidates`/`select_candidate` 明确"需要已有候选/答案"；`retry_search` 明确"只在 ERROR 时用"；并加"无法执行时不要硬选动作"。
2. **允许"空计划"表达**：`ShadowPlan` 支持空 steps（source=`unplannable`），`review_shadow_plan` 对空计划返回特殊 allow（code=`unplannable`），日志记录"模型认为无合法动作"。这样模型有诚实出口，不必硬塞错误动作。

**验证**：改后重跑矩阵，预期 WAIT_CHAPTER 的 `explainable_failure_required` 大幅下降，被拒的变成 `unplannable`（语义正确）或正确动作（如 set_chapter/global_search/continue_search）。

### 实现：规划器加入改写层（一次调用，先改写再规划）
用户指出根因：**用户意图模糊是本质，只改 prompt 教模型选动作是治标**。规划器在判断意图前先加一层"补齐"——LLM 先改写模糊请求（补省略、还原指代、加关键词、写明原因），再基于改写后的完整表述提计划。

实现（一次模型调用，输出 JSON 同时含改写 + 计划）：
- `shadow_plan_v0.py` 新增 `ShadowPlannerResult`（`rewritten_text`/`keywords`/`reason`/`plan`）。
- `shadow_planner_v0.py` prompt 加"第一步改写、第二步规划"指令；`parse_shadow_plan_v0` 解析复合结构；`plan()` 返回 `ShadowPlannerResult | None`。
- `agent.py` `_maybe_shadow_plan` 解包结果，日志新增 `rewritten` 字段（改写文本/关键词/理由）。
- `shadow_plan_log.py` `ShadowPlanLogEntry` 加 `rewritten`。
- `evaluate_shadow_plan_qwen_v0.py` 记录改写信息。
- 保留"空计划 unplannable"：改写后仍判断无合法只读动作时，输出空 steps（source=unplannable），review 返回特殊 allow（code=unplannable），日志区分"无法规划"与"被拒"。

测试：`test_tiku_agent_shadow_planner_v0.py` 重写覆盖复合解析/空步骤/缺 rewritten/脱敏；agent 测试 5 个 stub 改返回 `ShadowPlannerResult`；`shadow_plan_v0` 加 unplannable review 测试。相关 89 条、全量 372 条通过。

验证命令：
```powershell
python -B -m unittest tests.test_tiku_agent_shadow_planner_v0 tests.test_tiku_agent_shadow_plan_v0 tests.test_tiku_agent_agent   # 89/89
python -B -m unittest discover -s tests -p "test_*.py"   # 372 全量
PYTHONIOENCODING=utf-8 python -B scripts/evaluate_shadow_plan_qwen_v0.py   # 重跑矩阵对比（关键：WAIT_CHAPTER explain_failure 应下降）
```

### 改写层两轮矩阵结果 + 诊断修复
**首轮改写矩阵**（`.tmp_shadow_plan_eval_8794/20260801_191137/`）：总通过率 90.5%（95/105），但结构合法率降到 92.4%（8 条 planner_unavailable）、unplannable 41 条。逐格分析：
- unplannable 41 条**大多合理**（IDLE 阶段 14 条几乎全 unplannable 正确；"算了不看了/就这样吧"放弃类正确），少数可疑（WAIT_CHAPTER"那换一道难一点的吧"其实可 global_search）。
- planner_unavailable 8 条是**评估脚本盲区**——`plan()` 吞掉异常，解析失败原因丢失。

**修复评估脚本盲区**：加 `_dashscope_raw`（直接调 API 拿原始文本）+ `_json_or_raw`，records.jsonl 新增 `raw_output` 字段，解析失败不再黑盒。

**诊断出真因（第二轮矩阵，93.3% 通过率，5 条解析失败）**：查看 raw_output 发现**模型改写 reason 写得过长**（每条几百字推理），导致 JSON 超出 max_tokens=512 被截断，解析失败。这是改写 prompt 的副作用。
**修复**：① prompt 明确"reason 只写一句话不超过 30 字，不要展开推理"；② max_tokens 512→768 兜底。相关测试全量 372 通过。

改动文件：
- `tiku_agent/shadow_planner_v0.py`（prompt reason 限长 + max_tokens）
- `scripts/evaluate_shadow_plan_qwen_v0.py`（raw_output 诊断 + max_tokens）

验证命令：
```powershell
python -B -m unittest discover -s tests -p "test_*.py"   # 372 全量
PYTHONIOENCODING=utf-8 python -B scripts/evaluate_shadow_plan_qwen_v0.py   # 第三轮矩阵
```

### 第三轮改写矩阵：105/105 全通过（`.tmp_shadow_plan_eval_8794/20260801_193049/`）
- **结构合法率 100%**（reason 限长 + max_tokens 修复后，解析失败归零）。
- **review 全 allow：105/105**，无 reject，无 planner_unavailable。
- unplannable 57 条 / 真计划 48 条。逐条核对：**绝大多数合理**——
  - IDLE 15 条全 unplannable（无题图无任务，没有可执行只读动作，正确）。
  - 所有阶段"算了不看了/就这样吧"（放弃）unplannable，正确。
  - "有没有更接近的"在 WAIT_QUESTION_CHOICE 虽可换题，但 `select_question` 需用户指定题号——模型诚实标记"信息不足"，**是正确的谨慎，不是过度保守**。影子规划职责正是识别"信息不足不可执行"，而非替用户猜。
- 结论：**不修改 unplannable 行为**。它与设计意图一致，比旧版硬塞 explain_failure 更诚实。

### 改写层完整演进（供 Codex 复核全过程）
| 版本 | 总通过率 | 结构合法 | 说明 |
|---|---|---|---|
| 无改写基线 | 56.2% | 100% | 动作级预算已修复 |
| 改写层 v1 | 90.5% | 92.4% | unplannable 41；8 条解析失败（盲区） |
| + raw_output 诊断 | 93.3% | 95.2% | 定位真因：reason 过长超 max_tokens |
| + reason 限长 & max_tokens=768 | **100%** | **100%** | 全阶段 15/15 |

---

## 步骤：Stage 5 第一次审查修正——真实入口金标准评估

### 干了啥

根据 Codex 对 `f68431e..e6bb9a9` 的审查，先修评估尺子，不改 Planner Prompt、不拆工具、不开放执行：

- 保留 `scripts/evaluate_shadow_plan_qwen_v0.py` 为 Planner 结构压力测试；它直接调用 Planner，不再代表真实入口通过率。
- 新增 `scripts/evaluate_shadow_plan_entry_qwen_v0.py`，每条样本都从 `TikuSearchAgent.handle_text()` 进入，真实经过安全回答边界、固定业务意图识别和 `ambiguous_*` Planner 准入门。
- 每条样本从同一个 `AgentState` 成对运行：关闭影子规划 / 开启影子规划；两边共享同一份意图模型返回缓存，避免模型波动制造假差异。
- 使用确定性只读工具替身，不读取题库、不写业务数据；比较用户回答、业务状态与原工具调用序列是否完全一致。
- 新增35条人工金标准，明确允许路线和禁止推断动作；“就这样吧/哪个靠谱/别的那个”等不能被补成选择题目或候选。
- 结果分类为 `safe_answer/fixed_business/fixed_clarification/shadow_actionable/needs_confirmation/unplannable/permission_rejected/planner_unavailable`；空计划不再计入有动作规划通过率。
- CLI 默认真实运行3轮并逐条打印进度，`--model/--endpoint` 会实际透传给 Intent 与 Planner 两个真实客户端。

### 改动文件

- `scripts/evaluate_shadow_plan_entry_qwen_v0.py`（新增）
- `tests/fixtures/shadow_plan_entry_v0_cases.json`（新增，35条金标准）
- `tests/test_tiku_agent_shadow_entry_eval_v0.py`（新增）
- `.agents/roadmap.md`
- `.agents/project_memory.md`
- `.agents/8794_handoff.md`

### 真实三轮结果

输出目录：`.tmp_shadow_plan_entry_eval_8794/20260801_201645/`（忽略目录，不提交）。

- 总体：91/105（86.67%）；三轮分别31/35、30/35、30/35。
- 固定明确路径误触发 Planner：0。
- 开关影子规划后的回答、业务状态和原工具调用差异：0。
- 路线分布：安全回答24、固定业务52、有动作影子计划10、空计划16、权限拒绝3。
- `needs_confirmation`：0——说明当前协议仍缺真正的确认语义。
- 禁止语义动作命中14次：
  - “就这样吧”自动选题/候选：3次；
  - “你说哪个靠谱”自动选候选：2次；
  - “别的那个也行”自动选候选：1次；
  - 等章节询问“哪一章”被固定Intent误判为拒绝：2次；
  - 已答后“有没有更接近的”被固定Intent直接拒绝整批候选：3次；
  - 候选阶段“刚才那个是不是不对”被Planner提议为答案不匹配（权限层随后拒绝）：3次。

这组结果确认：旧矩阵的105/105仅代表结构/权限契约，不代表真实语义规划全部正确。当前仍为纯影子记录，无工具执行和用户可见副作用。

### 验证命令

```powershell
python -B -m unittest tests.test_tiku_agent_shadow_entry_eval_v0 tests.test_tiku_agent_shadow_plan_v0 tests.test_tiku_agent_shadow_planner_v0 tests.test_tiku_agent_agent
python -B -m unittest discover -s tests -p "test_*.py"
$env:PYTHONIOENCODING = "utf-8"; python -B scripts\evaluate_shadow_plan_entry_qwen_v0.py --runs 3
```

### 剩余任务 / 下一步

- 只进入下一小步：改写/关键词补充结果增加 `explicit_keywords/inferred_keywords/evidence/confidence/requires_confirmation`。
- 代码要求题号、候选编号、章节和全局搜索授权必须有原话明确证据；否则只记录 `needs_confirmation`，不得产生可执行动作。
- 修复后复跑同一35条金标准三轮；禁止动作、固定路径Planner误触发和开关可见差异必须均为0。
- 暂不拆工具、不进入Stage 6、不提升8790。

---

## 步骤：Stage 5 影子语义授权闸门

### 干了啥

- 新增纯代码`shadow_semantic_gate_v0`：不调用模型、不调用工具、不修改状态；逐步骤核对Planner动作能否在用户原话中找到明确授权。
- 覆盖全部11个Planner动作。选择题号/候选必须有一致编号和选择表达；章节必须明确命名且不能由相邻章节子串误授权；全局搜索、继续搜索、候选否定、答案不匹配、重试等均有各自正向证据。“不要选第二题/不要全局搜/先不要继续搜”等否定表达优先，动作词出现也不算授权。
- “就这样吧/你说哪个靠谱/别的那个也行/这个你帮我看看/刚才那个是不是不对”等只能成为`needs_confirmation`或空计划，模型补出来的“选择/候选2”等关键词不能充当授权。
- 影子日志schema升至v2；`rewritten`新增`explicit_keywords/inferred_keywords/evidence/confidence/requires_confirmation`。其中证据和确定性由代码派生，不信任模型自报。
- 状态与参数权限仍是第一层硬边界；只有状态合法的计划才用语义闸门决定`allow`或`needs_confirmation`。用户可见回复、业务状态和原工具调用完全不变。
- 真实入口评估拆开统计固定业务误判、Planner原始越权提议、已拦截提议和实际放行越权，避免把“模型想错但代码挡住”继续算成执行违规。

### 改动文件

- `tiku_agent/shadow_semantic_gate_v0.py`（新增）
- `tiku_agent/agent.py`
- `tiku_agent/shadow_plan_log.py`
- `scripts/evaluate_shadow_plan_entry_qwen_v0.py`
- `tests/test_tiku_agent_shadow_semantic_gate_v0.py`（新增）
- `tests/test_tiku_agent_shadow_entry_eval_v0.py`
- `.agents/roadmap.md`
- `.agents/project_memory.md`
- `.agents/8794_handoff.md`

### 真实三轮结果

最终输出目录：`.tmp_shadow_plan_entry_eval_8794/20260801_204300/`（忽略目录，不提交）。

- 总体99/105（94.29%），三轮稳定为33/35。
- 固定明确路径误触发Planner：0。
- 开关影子后的回答、业务状态、原工具调用差异：0。
- Planner原始禁止动作提议：10；闸门已拦截：10；实际放行越权：0。
- `needs_confirmation`：11；空计划13；状态权限拒绝3；Planner不可用0。
- 剩余6次失败全部来自既有固定业务路由，两条样本每轮稳定复现：
  - `which_chapter`：“帮我看看这题哪一章的”被固定Intent拒绝；
  - `closer_after_answer`：“有没有更接近的”在ANSWERED被固定Intent直接`reject_candidates`。
- 额外6条明确组合动作探针全部走固定业务，没有进入Planner。当前真实入口可执行影子计划为0，说明下一步要讨论复杂请求准入，而不是放松语义闸门。

### 验证命令

```powershell
python -B -m unittest tests.test_tiku_agent_shadow_semantic_gate_v0 tests.test_tiku_agent_shadow_entry_eval_v0 tests.test_tiku_agent_shadow_plan_v0 tests.test_tiku_agent_shadow_planner_v0 tests.test_tiku_agent_agent
python -B -m unittest discover -s tests -p "test_*.py"
$env:PYTHONIOENCODING = "utf-8"; python -B scripts\evaluate_shadow_plan_entry_qwen_v0.py --runs 3
```

### 剩余任务 / 下一步

- 先讨论Planner准入范围：是否仅增加“原话明确包含两个及以上动作”的影子观察入口；固定业务仍照常响应和执行。
- 为新准入增加可执行计划金标准，证明语义闸门不是全部拒绝，同时保持单动作快速路径不调用Planner。
- 两条固定Intent误判单独处理；暂不拆工具、不进入Stage 6、不提升8790。

---

## 步骤：Stage 5 复杂请求Planner准入基线

### 干了啥

- 新增40条平衡准入数据集：原子固定动作、明确顺序请求、条件请求、澄清/暂不支持各10条；字段包含`expected_entry/required_steps/forbidden_actions`。
- 新增独立评估器，严格区分两条数据：
  - 真实入口：从`Agent.handle_text()`成对比较开关影子规划，记录当前是否真的进入Planner、固定Intent、工具调用和可见差异；
  - 诊断Planner：对同一原始状态额外调用Planner，判断它能否补全固定单动作没有表达的步骤。诊断结果绝不冒充真实准入，也不执行计划。
- 统计原子误入、复杂请求准入召回、步骤顺序覆盖、状态权限、语义授权、禁止动作、条件授权风险和“未来可准入”顺序计划。
- 支持`--group`聚焦重跑单一类别；默认基线不因当前准入缺口返回失败，`--strict`留给未来准入实现后的验收。
- 本步骤未修改`agent.py`、Intent、Planner Prompt、语义闸门或运行时准入，用户体验没有变化。

### 改动文件

- `tests/fixtures/shadow_admission_v0_cases.json`（新增，40条）
- `scripts/evaluate_shadow_admission_qwen_v0.py`（新增）
- `tests/test_tiku_agent_shadow_admission_eval_v0.py`（新增）
- `.agents/roadmap.md`
- `.agents/project_memory.md`
- `.agents/8794_handoff.md`

### 真实结果

完整三轮输出：`.tmp_shadow_admission_eval_8794/20260801_211726/`（忽略目录，不提交）。

- 总计120次；原子快速路径30次、顺序30次、条件30次、澄清/暂不支持30次。
- 应观察的顺序+条件60次，真实入口进入Planner：0；当前准入召回0%。
- 原子快速路径误入Planner：0；开关影子后的回答/状态/原工具差异：0。
- 独立诊断Planner完整覆盖复杂目标36/60次（60%）；实际禁止动作放行0。

修正一条ANSWERED候选编号后聚焦复验：

- 顺序组：`.tmp_shadow_admission_eval_8794/20260801_212301/`。6/10类连续三轮完整规划并通过当前权限/语义闸门，共18/30；其余4类为排除+续搜只保留续搜、连续两次选题只保留一次、章节搜索后的未来候选选择被安全延后。
- 稳定6类：重新展示候选→选择候选、答案不匹配→回候选、重发答案→回候选、解释失败→重试、继续搜索→展示新候选，以及ANSWERED回候选→选择候选。
- 条件组：`.tmp_shadow_admission_eval_8794/20260801_212528/`。目标步骤覆盖20/30，但`future_admission_ready=0`；25/30被现有语义闸门视为直接授权风险。
- 更严重的是固定Intent已先吃掉条件句：27/30直接形成业务动作/响应，12/30调用了检索工具。例如“如果还能重试，就再试一次”直接重跑完整检索链，“如果这题适合力法，就按力法搜”直接搜索。

### 结论

- 不能直接把“所有明确复杂请求”送入未来执行；顺序与条件必须拆开。
- 顺序请求存在可验证价值，可作为后续首批纯影子准入候选。
- 条件请求当前只适合观测。Plan协议没有条件/分支前提，固定Intent和语义闸门也没有保护“条件未成立”；先修条件保护，再谈准入。
- 这一步由测试结果决定方向，没有设置模型置信度阈值，也没有实现模型反问。

### 验证命令

```powershell
python -B -m unittest tests.test_tiku_agent_shadow_admission_eval_v0
python -B -m unittest discover -s tests -p "test_*.py"
$env:PYTHONIOENCODING = "utf-8"; python -u -B scripts\evaluate_shadow_admission_qwen_v0.py --runs 3
$env:PYTHONIOENCODING = "utf-8"; python -u -B scripts\evaluate_shadow_admission_qwen_v0.py --runs 3 --group sequential
$env:PYTHONIOENCODING = "utf-8"; python -u -B scripts\evaluate_shadow_admission_qwen_v0.py --runs 3 --group conditional
```

### 剩余任务 / 下一步

- 下一步先讨论并建立条件表达保护测试，不直接实现顺序准入。
- 条件保护必须同时覆盖固定Intent和语义闸门，且不能破坏明确无条件指令。
- 条件保护通过后，再扩充顺序请求留出集并实现第一批纯影子准入；暂不进入Stage 6、不拆工具、不提升8790。

---

## 步骤：Stage 5 Agent流量权重与安全硬门槛

### 干了啥

- 只读核对8790/8793生产脱敏日志：8790共188轮/51会话，8793共119轮/24会话；日志能统计动作、阶段和工具，但隐私边界禁止保存用户原话，不能据此还原多动作/条件句的真实语言频率。
- 保留40条四组均衡数据作为边界压力集，新增`representative_v0`暂定产品权重：原子70%、含糊/暂不支持20%、顺序8%、条件2%。前两类为流量大头有脱敏动作日志支撑，8%/2%明确标记为产品先验。
- 新增加权入口契约分，但不让低频危险被平均：原子误入、可见差异、实际放行禁止动作、条件请求直接工具调用均为零容忍硬门槛。
- 旧完整120轮按新口径为90%入口契约分、`release_ready=false`；条件专项30轮中12轮调用工具（39次调用），安全硬门槛失败。

### 改动文件

- `tests/fixtures/shadow_admission_v0_cases.json`
- `scripts/evaluate_shadow_admission_qwen_v0.py`
- `tests/test_tiku_agent_shadow_admission_eval_v0.py`
- `.agents/roadmap.md`
- `.agents/project_memory.md`
- `.agents/8794_handoff.md`

### 验证命令

```powershell
python -B -m unittest tests.test_tiku_agent_shadow_admission_eval_v0
python -B -m unittest discover -s tests -p "test_*.py"
```

结果：专项9/9、全量402/402通过。全量测试在受限沙箱内会因Python临时目录权限产生1个环境假失败，沙箱外原命令复跑通过。

### 下一步

- 先建立条件表达保护金标准并修固定Intent/语义闸门，保证条件未满足时不直接执行；权重只用于评估产品影响，不能替代安全门槛。
- 若要把8%/2%替换成实测频率，应在8794另行评审隐私保护的在线粗分类日志，只存类别与阶段、不存用户原话；不得从现有日志伪造频率。

---

## 步骤：Stage 5 条件表达保护

### 干了啥

- 新增纯代码`conditional_guard_v0`，固定Intent和影子语义闸门共用同一判断，不调用模型、不执行工具、不修改状态。
- 代码可验证三类现有事实：错误是否可重试、是否还有下一批候选、当前是否确为未命中；条件成立时提取后件并保留原固定快速路径，未成立时不执行。
- “候选1不对/答案不匹配/题目适合力法/第二题是目标”等需要用户或模型判断的前提一律视为未解决：Intent返回澄清并允许影子Planner观察，语义闸门返回`needs_confirmation`。
- “方便的话/如果方便/可以的话”等纯礼貌表达不作为业务前提；普通无条件指令保持原路由。
- 准入评估区分全部条件工具调用与“未解决条件工具调用”，并把状态可验证条件误进Planner加入硬门槛；只跑单组时不再声称整体`release_ready=true`。

### 改动文件

- `tiku_agent/conditional_guard_v0.py`（新增）
- `tiku_agent/intent_v2.py`
- `tiku_agent/shadow_semantic_gate_v0.py`
- `scripts/evaluate_shadow_admission_qwen_v0.py`
- `tests/fixtures/shadow_admission_v0_cases.json`
- `tests/test_tiku_agent_conditional_guard_v0.py`（新增）
- `tests/test_tiku_agent_shadow_admission_eval_v0.py`
- `.agents/roadmap.md`
- `.agents/project_memory.md`
- `.agents/8794_handoff.md`

### 真实验证

- 输出：`.tmp_shadow_admission_eval_8794/20260801_224524/`（忽略目录，不提交）。
- 30次条件矩阵：21/21未解决条件进入影子观察，9/9状态可验证条件不进Planner；未解决条件工具回合0、语义误放行0、可见差异0、入口契约失败0。
- 使用未改三组旧基线与新条件组组合复算：完整四组入口契约分92%，硬门槛全过；顺序组仍为0%，所以整体`release_ready=false`。

### 验证命令

```powershell
python -B -m unittest tests.test_tiku_agent_conditional_guard_v0 tests.test_tiku_agent_shadow_admission_eval_v0 tests.test_tiku_agent_shadow_entry_eval_v0 tests.test_tiku_agent_intent_v2 tests.test_tiku_agent_shadow_semantic_gate_v0
$env:PYTHONIOENCODING='utf-8'; python -u -B scripts\evaluate_shadow_admission_qwen_v0.py --runs 3 --group conditional
python -B -m unittest discover -s tests -p "test_*.py"
```

结果：专项68/68、全量412/412通过；真实条件矩阵30/30符合入口契约。

### 下一步

- 扩充6类稳定顺序请求的独立留出集；只在留出集证明稳定后实现纯影子准入，仍不执行Planner动作。

---

## 步骤：Stage 5 顺序请求独立留出与确认

### 干了啥

- 新增24条独立初始留出：6类顺序请求各3条全新说法，加6条单动作保护反例；与原40条开发集无文本重复。
- 初始真实千问1轮为14/18正例可准入，只有3/6类全过。4条失败拆为：1条ANSWERED候选3越界的测试数据错误、2条Planner完整但候选展示同义词未过语义闸门、1条“续搜后展示”Planner漏掉展示步骤。
- 修正越界数据；固定Intent与语义闸门补“候选名单发回来/候选页调回来/候选清单调出来/切回候选页”等明确展示证据。初始Planner漏步骤样本保留，不改Prompt刷分。
- 另建15条全新确认集：5类×2条正例+5条单动作反例，明确排除已漏步骤的`continue_then_show`。三轮45次中正例21/30；只有`show_then_select`与`report_then_show`各6/6稳定，其余3类各3/6。
- 确认集单动作“请重发解答”3/3误进现有Planner，原因是固定Intent只认“答案”不认“解答/结果”；已修为三种答案对象并补测试。本轮仍未开放任何新运行时Planner准入。

### 改动文件

- `tests/fixtures/shadow_sequential_holdout_v0_cases.json`（新增）
- `tests/fixtures/shadow_sequential_confirmation_v0_cases.json`（新增）
- `scripts/evaluate_shadow_sequential_holdout_qwen_v0.py`（新增）
- `tests/test_tiku_agent_shadow_sequential_holdout_v0.py`（新增）
- `tiku_agent/intent_v2.py`
- `tiku_agent/shadow_semantic_gate_v0.py`
- `tests/test_tiku_agent_intent_v2.py`
- `tests/test_tiku_agent_shadow_semantic_gate_v0.py`
- `.agents/roadmap.md`
- `.agents/project_memory.md`
- `.agents/8794_handoff.md`

### 真实验证

- 初始留出：`.tmp_shadow_sequential_holdout_eval_8794/20260801_233151/`，18条正例14条可准入，原子误入0、可见差异0、实际放行禁止动作0。
- 全新确认：`.tmp_shadow_sequential_holdout_eval_8794/20260801_233839/`，30次正例21次可准入；稳定类2/5，原子误入3次均来自同一“请重发解答”并已修复；可见差异0、实际放行禁止动作0。

### 验证命令

```powershell
python -B -m unittest tests.test_tiku_agent_shadow_sequential_holdout_v0 tests.test_tiku_agent_intent_v2 tests.test_tiku_agent_shadow_semantic_gate_v0 tests.test_tiku_agent_shadow_admission_eval_v0
$env:PYTHONIOENCODING='utf-8'; python -u -B scripts\evaluate_shadow_sequential_holdout_qwen_v0.py --runs 1
$env:PYTHONIOENCODING='utf-8'; python -u -B scripts\evaluate_shadow_sequential_holdout_qwen_v0.py --fixture tests\fixtures\shadow_sequential_confirmation_v0_cases.json --runs 3
python -B -m unittest discover -s tests -p "test_*.py"
```

结果：专项58/58、全量418/418通过；运行时Planner准入逻辑未改。

### 下一步

- 只为2个稳定类建立纯代码顺序准入识别器与反例金标准；先离线评估，不直接改`Agent.handle_text()`入口。

---

## 步骤：Stage 5 两类顺序请求离线准入识别

### 干了啥

- 新增纯代码`shadow_sequential_admission_v0`，只识别两个已在全新确认集稳定的类别：`WAIT_CANDIDATE_CHOICE`阶段“展示候选→选择明确编号候选”，以及`ANSWERED`阶段“明确报告答案不匹配→返回候选”。
- 要求两个动作各自有原话证据、顺序正确且中间有明确连接词；条件句、否定、疑问/不确定、倒序、单动作、模糊候选编号和错误阶段全部拒绝。
- 特意不接受`ANSWERED`阶段“返回候选→再选择”，因为该类在真实确认矩阵只有3/6，不用代码规则掩盖Planner不稳定。
- 新增离线评估器，复用开发集、独立留出集和全新确认集；不调用模型、不执行工具、不修改状态。
- 本轮未修改`Agent.handle_text()`、Planner Prompt、运行时准入或用户响应，识别器尚未上线。

### 改动文件

- `tiku_agent/shadow_sequential_admission_v0.py`（新增）
- `scripts/evaluate_shadow_sequential_admission_v0.py`（新增）
- `tests/test_tiku_agent_shadow_sequential_admission_v0.py`（新增）
- `.agents/roadmap.md`
- `.agents/project_memory.md`
- `.agents/8794_handoff.md`

### 验证结果

- 离线三组共79条：12条正例全部识别，67条反例全部拒绝；开发集2/2、留出集6/6、确认集4/4，无假阳性、无假阴性。
- 专项20/20通过；整仓424/424通过。受限沙箱首次全量运行仍触发既有Windows临时目录权限假失败，沙箱外原命令复跑全过。
- 结论：具备下一轮“接入纯影子观察”的资格，不代表具备执行Planner动作或发布资格。

### 验证命令

```powershell
python scripts\evaluate_shadow_sequential_admission_v0.py
python -m unittest tests.test_tiku_agent_shadow_sequential_admission_v0 tests.test_tiku_agent_conditional_guard_v0 tests.test_tiku_agent_shadow_sequential_holdout_v0 -v
python -m unittest discover -s tests -p "test*.py"
```

### 下一步

- 把识别器接到纯影子入口：只有它接受的两类才允许Planner观察，仍不执行计划。
- 成对验证影子开关下回答、状态、原工具调用完全一致；识别器拒绝的所有类别不得进入Planner。
