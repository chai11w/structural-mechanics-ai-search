# 8794 候选线路交接文档

> Claude Code 负责实现，Codex 负责审查。每完成一步更新本文件并提交。
> 审查流程：读本文件 → 按"改动文件"清单逐个看 git diff → 运行"验证命令"确认结果。

## 当前进度

- **目标**：8794 状态感知安全回答（roadmap Stage 4）——让安全回答能感知当前业务阶段（等待章节/候选选择/答案展示），业务操作仍走固定状态机。
- **总计划**：M1 状态摘要模块 → M2 prompt 契约 → M4 generator+agent 接线 → M3 接线后按需兜底文案 → M5 runtime 验收测试 → M6 离线状态感知矩阵评估 + 反射指令强化。
- **已完成**：M1–M6 全部完成。状态感知安全回答（roadmap Stage 4）实现、验收与真实模型效果评估完成。
- **下一步**：交 8793 评审（review）后再提升 8790。离线状态感知评估已随 M6 落地（`--matrix` 模式，连真实 Qwen，context 全程透传）。

---

## 已完成步骤

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
