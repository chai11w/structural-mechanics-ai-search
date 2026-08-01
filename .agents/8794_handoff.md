# 8794 候选线路交接文档

> Claude Code 负责实现，Codex 负责审查。每完成一步更新本文件并提交。
> 审查流程：读本文件 → 按"改动文件"清单逐个看 git diff → 运行"验证命令"确认结果。

## 当前进度

- **目标**：8794 状态感知安全回答（roadmap Stage 4）——让安全回答能感知当前业务阶段（等待章节/候选选择/答案展示），业务操作仍走固定状态机。
- **总计划**：M1 状态摘要模块 → M2 prompt 契约 → M4 generator+agent 接线 → M3 接线后按需兜底文案 → M5 runtime 验收测试。
- **已完成**：M1 状态摘要模块、M2 prompt 契约扩展、M4 generator+agent 接线、M3 接线后按需兜底文案（机制落地 + 空表）。
- **下一步**：M5 runtime 级验收测试（候选阶段寒暄、等待章节致谢、答案后寒暄、业务不误入、跨重启恢复、跨用户隔离）。

---

## 已完成步骤

### M3 接线后按需兜底文案（空表机制）
`render_safe_answer_v0` 增加 `_PHASE_REPLY_BUILDERS[(category, phase)]` 查询：命中 builder 则用过输出契约的 phase 文案；否则回落 `_SAFE_REPLIES` 通用兜底。表**有意保持为空**——各阶段业务引导已由 render.py 状态机承担（章节提示/全局搜索/错误重试/候选列表），纯寒暄兜底不需要 phase 专用文案；模型失败时永远回落合法单行回答，不会无回答。新增 3 条边界测试：空表逐 phase 回落、命中仅覆盖本 (category, phase)、builder 违反契约回落。

### M1 状态摘要模块（commit `82634dd`）
新增纯模块 `safe_answer_context_v0.py`，从 `AgentState` 派生脱敏白名单摘要 `SafeConversationContext`。allowed_actions 动态权限矩阵派生；phase 英文常量、waiting_for/last_completed_step 中文；路径/分数/错误/session_id 不入白名单；IDLE 无状态小节。18 个单测 + 全量通过。

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
