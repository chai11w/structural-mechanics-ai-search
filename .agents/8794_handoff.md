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
