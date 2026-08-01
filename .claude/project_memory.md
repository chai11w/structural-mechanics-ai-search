# 项目记忆 — 8794 状态感知安全回答（roadmap Stage 4）

> 权威交接文档：`.agents/8794_handoff.md`（Claude Code 实现 → Codex 审查流程，逐步骤记录）。
> 本文件是新对话快速接手的精简入口；与 handoff 冲突时以 handoff 为准。

## 与 Codex 的协作规则（每步都必须遵守）

- **分工**：Claude Code 负责实现，Codex 负责审查。二者都在 `F:\cc\7-题库检索-8794` 这个 worktree、`codex/8794-candidate-v1` 分支上工作。
- **交接文档是唯一权威**：每完成一步，Claude Code 更新 `.agents/8794_handoff.md`（含干了啥/改动文件/验证命令与结果/审查关注点/剩余任务/建议下一步命令），并 commit + push 到 GitHub。
- **审查流程**（Codex 侧）：读 handoff → 按"改动文件"清单逐个 `git diff` → 运行"验证命令"确认结果。
- **审查提示要短**：Claude Code 给 Codex 的 review prompt 只指向 handoff 文档路径即可，不要长篇复述内容。
- **换对话交接**：更新 project_memory + handoff，提交推送；不留未提交的脏改动。
- 分支上可能已有 Codex 的额外提交（如收紧安全回答校验、候选重选支持等），动手前先 `git log --oneline -10` 看最新状态，别把别人的提交当自己的工作区改动覆盖。

## Current State

- 分支：`codex/8794-candidate-v1`（8794 候选 worktree，与 8790 主线路隔离，端口 8794）。
- **M1–M6 全部完成**，roadmap Stage 4（状态感知安全回答）实现 + 验收 + 真实模型效果评估闭环。
- 最新 commit：`c1d90a3`（M6：状态感知矩阵评估 + 寒暄反射指令），已推送 GitHub。
- 8794 服务当前**正在运行**：`127.0.0.1:8794`（PID 33456，后台 `python -B scripts/run_tiku_agent_8794.py --port 8794`），默认启用安全回答。浏览器 `http://127.0.0.1:8794` 可试。
- 8790 主线路（端口 8790）与飞书桥（8787/8788）也常驻运行，与本工作隔离。

## Implemented

- M1 状态摘要纯模块 `safe_answer_context_v0.py`（脱敏白名单 `SafeConversationContext`）。
- M2 prompt 契约扩展（`build_safe_answer_prompt_v0(context=None)`，无 context 字节级不变）。
- M3 phase 兜底机制 `_PHASE_REPLY_BUILDERS`（**有意为空**，业务话术留在 render.py 状态机）。
- M4 generator+agent 接线（`generate(text, context)` 透传；`_safe_answer_context()` 异常降级为 state-free）。
- M5 runtime 验收测试（`tests/test_tiku_agent_safe_answer_state_aware.py`，11 条黑盒）。
- M6 离线矩阵评估 + 反射指令：
  - `scripts/evaluate_safe_answer_qwen_v0.py --matrix`（7 阶段 × 10 话术 = 70 组合，连真实 Qwen）。
  - `safe_answer_contract_v0.py` 新增 `SAFE_ANSWER_STATE_REFLECT_V0`：按「等待」字段显式映射，让纯寒暄也状态感知。最终 70/70 契约通过（4 条仅因模型自问句带问号被 `unsolicited_question` 拦回落，属安全兜底正常）。

## In Progress / Not Implemented

- 无。Stage 4 收官。剩余是**流程动作**而非实现：
  - 交 8793 评审（审查入口：`.agents/8794_handoff.md`，M1→M6 顺序逐个 `git show <commit>`）。
  - 评审通过后再提升 8790。

## Known Risks / Decisions

- 用户明确拍板：`_PHASE_REPLY_BUILDERS` 保持空表（业务引导由 render.py 承担）；业务层 render.py 措辞不改（"重试"等原文保留）；反射指令迭代到此定版，不再调问号拦截率。
- 安全回答契约：单行 ≤90 字、无问号、无被禁执行动词（搜索/检索/找到/查到/读取/修改等）、不泄露路径/session_id/分数。
- API key 一律从环境变量读（`DASHSCOPE_API_KEY`），禁写进 config 或代码。
- 题库根：`config.local.json` 的 `root`（`D:\桌面\答疑、帮做\结构力学\帮做`），各章节 + xlsx 索引齐备。

## Next Best Step

1. 新对话优先读 `.agents/8794_handoff.md`（含 M6 审查关注点）。
2. 若 Codex 评审：给出 handoff 文档路径即可，评审提示要**简短**（用户要求）。
3. 若继续开发：8794 已在跑，可连真实模型验证；改完记得补 handoff + 提交推送（用户惯例）。

## Do Not Do

- 不把 API key/token 写进任何被 git 跟踪的文件。
- 不把全局记忆写进项目文件（用户全局约定）。
- 不改业务层 render.py 的既有措辞（用户已拍板）。
- 不提交 `.vscode/` 等无关工具配置。
