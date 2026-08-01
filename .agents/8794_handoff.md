# 8794 候选线路交接文档

> Claude Code 负责实现，Codex 负责审查。每完成一步更新本文件并提交。
> 审查流程：读本文件 → 按"改动文件"清单逐个看 git diff → 运行"验证命令"确认结果。

## 当前进度

- **目标**：8794 状态感知安全回答（roadmap Stage 4）——让安全回答能感知当前业务阶段（等待章节/候选选择/答案展示），业务操作仍走固定状态机。
- **总计划**：M1 状态摘要模块 → M2 prompt 契约 → M3 phase 兜底文案 → M4 generator+agent 接线 → M5 runtime 验收测试。
- **已完成**：M1（本次）。
- **下一步**：M2 prompt 契约扩展。

---

## 步骤：M1 状态摘要模块

### 干了啥
新增纯模块 `safe_answer_context_v0.py`：从 `AgentState` 派生一份**脱敏状态白名单摘要** `SafeConversationContext`，供安全回答模型可见。核心决策：allowed_actions 用**动态权限矩阵**派生（遍历 `TASK_ACTIONS - {cancel, search_image}` 逐个过 `authorize_action_v2` 求 allow 集）；phase 给模型看英文常量、waiting_for/last_completed_step 用中文（不含被禁执行动词）；`selected_rank/selected_question`、路径、分数、错误原文、session_id 一律不入白名单；IDLE 阶段无状态小节。

### 改动文件
- `tiku_agent/safe_answer_context_v0.py` → **新增**：`SafeConversationContext`（frozen dataclass，11 个白名单字段）、`build_safe_answer_context(state)` 派生函数、`render_state_section(context)`（IDLE 返回空串）、`_WAITING_FOR`/`_LAST_COMPLETED_STEP` 11-phase 中文映射表、`_authorized_text_actions`（动态权限矩阵派生）。
- `tests/test_tiku_agent_safe_answer_context_v0.py` → **新增**：18 个用例，覆盖 IDLE 构建、各 phase allowed_actions 与权限矩阵一致、11-phase 映射无被禁动词、非法输入拒绝、白名单不泄漏敏感字段、payload 键恰为白名单。

### 验证命令与结果
```
python -B -m unittest tests.test_tiku_agent_safe_answer_context_v0     # 18/18 通过
python -B -m unittest discover -s tests -p "test_*.py"                 # 全量 OK，无回归
```

### 审查关注点
- `SafeConversationContext` 白名单字段是否严格限缩，无路径/候选记录/分数/错误原文/session_id 泄漏。
- `allowed_actions` 动态派生是否覆盖条件案例（global_search 仅 offered 时、continue_search 仅 continuation_available 时、retry_search 仅 ERROR+可重试）。
- `_WAITING_FOR`/`_LAST_COMPLETED_STEP` 文案是否不含被禁执行动词（搜索/检索/找到/查到/读取/修改/删除…）。
- `__post_init__` 校验是否足以保证模型永远收不到白名单外动作名。
- 模块是否纯（不 import Agent runtime、不 I/O、不改状态）。

### 剩余任务 / 已知风险
- 无已知风险。`build_safe_answer_context` 异常时由后续 M4 的 `_safe_answer_context()` 降级为 None（本步尚未接线，无调用方）。

### 建议下一步命令
- M2 实施前审查本步：`git show <m1-commit>`；运行上述两个验证命令。
