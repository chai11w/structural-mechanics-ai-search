# 8794 候选线路交接文档

> Claude Code 负责实现，Codex 负责审查。每完成一步更新本文件并提交。
> 审查流程：读本文件 → 按"改动文件"清单逐个看 git diff → 运行"验证命令"确认结果。

## 当前进度

- **目标**：8794 状态感知安全回答（roadmap Stage 4）——让安全回答能感知当前业务阶段（等待章节/候选选择/答案展示），业务操作仍走固定状态机。
- **总计划**：M1 状态摘要模块 → M2 prompt 契约 → M3 phase 兜底文案 → M4 generator+agent 接线 → M5 runtime 验收测试。
- **已完成**：M1 状态摘要模块、M2 prompt 契约扩展。
- **下一步**：M3 phase 感知兜底文案。

---

## 已完成步骤

### M1 状态摘要模块（commit `82634dd`）
新增纯模块 `safe_answer_context_v0.py`，从 `AgentState` 派生脱敏白名单摘要 `SafeConversationContext`。allowed_actions 动态权限矩阵派生；phase 英文常量、waiting_for/last_completed_step 中文；路径/分数/错误/session_id 不入白名单；IDLE 无状态小节。18 个单测 + 全量通过。

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
