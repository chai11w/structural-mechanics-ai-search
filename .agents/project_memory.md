# Project Memory

## Current State

- 项目位于 `F:\cc\7-题库检索`。
- 项目目标：维护结构力学题库，按题目图片或荷载描述检索相似题，并按排名复制/发送答案。
- 当前阶段：CLI、GUI、飞书检索、飞书入库、候选删除、章节 2-8、主库/字母库分流、字母库结构类型筛选都已进入可用维护阶段。
- 当前项目说明入口为 `AGENTS.md`、`.agents/project_memory.md`、`SKILL.md`、`README.md`。
- 旧 `.agents/skills/结构力学/` 已降级为跳转说明，不再作为当前流程来源。
- 当前验证以 `python scripts/smoke_test.py` 为主，必要时用真实题图或飞书 dry-run 抽查。
- `config.json`、`config.local.json` 被 `.gitignore` 忽略，可能包含本地路径或 API key；不要读取或提交完整内容。
- 本地 live 主库位于配置 `root` 指向的结构力学题库目录；独立字母库默认位于主库旁边的 `帮做_字母库`。

## Reading Order

1. `AGENTS.md`
2. `.agents/project_memory.md`
3. `SKILL.md`
4. `README.md`
5. `multi_agent_pipeline.py`
6. `scripts/feishu_tiku_bot.py`
7. `scripts/feishu_store_flow.py`
8. 任务相关脚本、配置示例和测试

默认不要读取全局记忆，也不要把全局偏好写入项目文件。

## Standing Collaboration Rule

- 用户要求：每次完成代码、题库逻辑或项目文档更改，都要同步更新项目记忆，并提交、推送到 GitHub。
- 常规收尾：更新文档/记忆 -> 运行验证 -> 检查 `git status` -> 只提交相关文件 -> `git commit` -> `git push`。
- 正式数据文件、运行日志、本地配置、密钥、大文件默认不提交；需要提交时先判断是否安全。
- 当前常见未提交运行产物：`data/feishu_chapter_failure_log.jsonl`，这是飞书章节判断日志，通常不要混进普通代码提交。

## 2026-07-10 Agent Workspace And Feishu Boundary

- User plans to build a new question-bank Agent before connecting it to Feishu.
- The Agent must be developed and run in a separate workspace/runtime boundary so it does not affect the existing Feishu question-bank bot state.
- The future Agent will connect to a new Feishu bot. Do not reuse the existing bot's app credentials, event URL, local port, cloudflared tunnel, session state, `.tmp_feishu_tiku` directory, or runtime logs unless the user explicitly requests a migration.
- Do not stop, restart, or reshape the current `scripts/feishu_tiku_bot.py` production-like service while building the Agent prototype. The first Agent version should be an independent entry point.
- New Agent runtime files must be separated by config/path:
  - checkpoints / LangGraph state;
  - Feishu event records;
  - downloaded/uploaded temporary images;
  - session/user state;
  - operation/audit logs;
  - test outputs.
- The existing live question bank may be used as a read-only retrieval data source during early Agent work.
- Any write operation to live bank data, including store/delete/path repair, must still follow `plan -> confirm -> execute`, create backups, and remain visibly separate from old Feishu bot state.
- This boundary is a project rule, not only an implementation detail. Future conversations should preserve it before choosing filenames, ports, state directories, or Feishu integration paths.

## 2026-07-10 Agent Retrieval Tool Layer MVP

- Added a new isolated package `tiku_agent/` for the future Agent tool layer. It is not wired into the existing Feishu bot.
- Default Agent runtime directory is `.tmp_tiku_agent`, not `.tmp_feishu_tiku`.
- Implemented first-version 7 coarse retrieval tools in `tiku_agent/tools.py`:
  - `analyze_image_tool`: Qwen-based image analysis for layout/chapter/load data, using an Agent-local Qwen cache.
  - `route_bank_tool`: wraps `RuleRouter` to choose main/symbolic/review lanes.
  - `classify_structure_tool`: structure type tool for symbolic image searches; skips non-symbolic routes and first uses text fast path.
  - `coarse_search_tool`: read-only coarse search. It does not write `_last_search.json` and uses `resolve_question_path(..., update_excel=False)` to avoid live Excel path repair side effects.
  - `rerank_candidates_tool`: reranks candidates and returns visible candidates only; it does not answer automatically.
  - `parse_candidate_action_tool`: parses candidate-page actions such as `1`, `-1`, and `0` based on the current state.
  - `answer_candidate_tool`: returns/copies the chosen candidate's answer into `.tmp_tiku_agent/answer_output`, not the existing configured `answer_output`.
- Added standard-library tests in `tests/test_tiku_agent_tools.py`; no pytest dependency is required.
- Verification run:
  - `python -B -m unittest tests.test_tiku_agent_tools` passed, 4 tests.
  - `python -B -c "from tiku_agent.tools import AgentToolConfig; print(AgentToolConfig().runtime_dir)"` printed `.tmp_tiku_agent`.
- Current scope:
  - Tool layer only; no LangGraph graph yet.
  - No new Feishu bot yet.
  - No existing Feishu runtime files, ports, tunnels, sessions, or `.tmp_feishu_tiku` state touched.
- Known local issue:
  - Running `py_compile`/normal imports in this Windows sandbox created locked `tiku_agent/__pycache__` temp pyc files that could not be removed due `Access denied`. Use `python -B` for future checks to avoid adding more pycache files.

## 2026-07-10 Agent Intent Layer MVP

- Added `tiku_agent/intent.py` as the first intent layer for the future Agent.
- Intent layer is now LLM-first and state-aware. It does not execute tools directly.
- Natural-language user input is sent to Qwen/DashScope for a fixed JSON intent; Python validation then checks state legality, chapter range, candidate/question bounds, and unsupported actions.
- Supported MVP intents:
  - `search_image`
  - `set_chapter`
  - `select_question`
  - `select_candidate`
  - `cancel`
  - `unsupported`
- Core functions:
  - `build_intent_prompt(...)`
  - `call_qwen_intent(...)`
  - `validate_intent_payload(...)`
  - `parse_user_intent(...)`
- Rule parsing remains only as an explicit fallback if the LLM call fails; the intended path is LLM -> validation -> Agent graph/tool selection.
- State-sensitive parsing is intentional:
  - `1` in `WAIT_QUESTION_CHOICE` means select question 1.
  - `1` in `WAIT_CANDIDATE_CHOICE` means select candidate 1.
  - `4` in `WAIT_CHAPTER` means `4力法`.
- The layer supports simple natural phrases such as `按力法`, `矩阵位移`, `选第一个`, text image paths, and `2-4力法` question/chapter override commands.
- Store/delete/repair phrases are explicitly rejected as `unsupported` for this Agent MVP. Those operations remain outside the first retrieval Agent scope.
- Added `tests/test_tiku_agent_intent.py`.
- Verification run:
  - `python -B -m unittest tests.test_tiku_agent_intent tests.test_tiku_agent_tools` passed, 18 tests.
  - Live Qwen intent smoke passed:
    - `第二题按力法` in `WAIT_QUESTION_CHOICE` -> `select_question`, `question_index=2`, `chapter_override=4力法`.
    - `给我第一个答案` in `WAIT_CANDIDATE_CHOICE` -> `select_candidate`, `rank=1`.
    - `删掉第一个` in `WAIT_CANDIDATE_CHOICE` -> rejected as `unsupported`, `requested_action=delete`.
- Current scope:
  - Intent parsing only.
  - No LangGraph graph yet.
  - No Feishu integration and no old Feishu runtime state touched.

## 2026-07-11 Agent State Layer MVP

- Added `tiku_agent/state.py` as the minimal state layer for the future retrieval Agent.
- The state layer is intentionally small and does not call LLMs, search tools, Feishu APIs, or existing Feishu runtime state.
- First version keeps exactly 11 core fields:
  - `session_id`
  - `state`
  - `image_path`
  - `chapter`
  - `loads`
  - `route`
  - `structure_type`
  - `candidates`
  - `selected_rank`
  - `questions`
  - `selected_question`
- Supported state gates include:
  - `IDLE`
  - `WAIT_CHAPTER`
  - `WAIT_QUESTION_CHOICE`
  - `WAIT_CANDIDATE_CHOICE`
  - `READY_TO_ROUTE`
  - `READY_FOR_SEARCH`
  - `DONE`
  - `CANCELLED`
  - `ERROR`
  - `NO_MATCH`
- Multi-question support is preserved at the state layer through `questions` and `selected_question`, but no multi-question orchestration has been added yet.
- `last_error`, answer output paths, event logs, layout details, chapter confidence/evidence, and full tool traces are intentionally not in the first state schema. Add them later only if orchestration, persistence, or debugging needs prove it.
- Added `tests/test_tiku_agent_state.py` using standard-library `unittest`.
- Current scope:
  - State data model only.
  - No orchestration layer yet.
  - No LangGraph graph yet.
  - No Feishu integration and no old Feishu runtime state touched.

## Supported Chapters

- `2静定结构`
- `3静定结构位移`
- `4力法`
- `5位移法`
- `6力矩分配`
- `7矩阵位移`
- `8影响线`

GUI、飞书手动章节、自动章节提示、入库、删除、审计和 smoke test 均应支持 2-8 章。

## Core Architecture

- `search.py`
  - 基础荷载相似度、路径解析、答案定位、基础 CLI。
  - `TOP_K` 默认是 `3`，但本地 `config.json` / `config.local.json` 的 `top_k` 会覆盖它。
  - 初筛规则：如果 100% 粗筛匹配数超过 `top_k`，保留全部 100%；否则补到 `top_k`。
- `multi_agent_pipeline.py`
  - Qwen 图片识别、章节判断、RuleRouter 路由、主库/字母库检索、Zhipu 复筛协调。
  - GUI 图片检索和飞书检索主要走这个 pipeline。
- `scripts/search_by_loads.py`
  - Skill/CLI 用的轻量包装：`loads-search`、`image-search`、`answer`。
- `gui.py`
  - Tkinter 桌面端，支持图片检索、手动荷载、预览、答案入口、入库和一键审查。
- `scripts/feishu_tiku_bot.py`
  - 飞书机器人入口，支持单题/多题检索、自动/手动章节、取答案、入库、删除、OK reaction。
- `scripts/feishu_store_flow.py`
  - 飞书新增题目入库，负责计划、编号、备份和 Excel 追加。
- `scripts/feishu_delete_flow.py`
  - 飞书候选错题删除，删除前备份 Excel 和图片。

## Retrieval Rules

- 章节必须由用户指定、确认，或由 `chapter=auto` 在题干/题型文字证据下自动确定。
- 不要跨章节盲搜。纯结构图、荷载、尺寸、支座、EI 不能自动猜章节。
- 荷载类型只使用 `集中`、`均布`、`弯矩`。
- `raw` 保留图中或用户描述的原始标注，但单位会归一化为无单位数字/表达。
- 默认粗筛 `top_k=3`；若 100% 粗筛匹配超过 3 个，全部进入候选。
- 复筛只作用于粗筛候选，不应反向改变初筛候选池。
- 复筛后的用户可见相似度使用 `final_score`。
- 复筛输出规则：
  - `final_score > 90%` 的结果全部输出；
  - 如果没有 `>90%`，输出 `>80%` 的前 3 个；
  - 如果一个都没有超过 80%，输出复筛分最高的 1 个。
- 上述 80/90 规则只用于复筛后的输出，不用于初筛，也不用于未复筛输出。
- `_last_search.json` 必须按最终显示结果重写，确保 `answer 1/2/3...` 对应用户看到的排名。

## Main Bank And Symbolic Bank

- 主库保存数值荷载题和已赋值符号题，例如 `P=40`、`q=20`。
- 字母库保存未赋值字母题，例如 `q`、`2P/a`、`M`。
- 字母库 Excel 列为：`题目名称`、`荷载`、`结构类型`。
- 字母库的 `raw` 写入相似度编码，`original_raw` 保留原始字母标注。
- 字母荷载按三个量纲体系归一化：均布力主题、集中力主题、集中弯矩主题。
- 长度符号不固定为 `L`，`a/b/l` 等字母都可作为长度占位。
- 字母荷载相似度会做同题体系冲突消解：同题内某主题体系占主导时，孤立冲突项仅在相似度内部归入主导体系，不改写原始 `raw`。
- 飞书新增字母题写入字母库时必须同步写入 `结构类型`，否则后续结构类型筛选可能漏掉新题。

## Structure Type Filtering

- 字母库结构类型枚举：`梁`、`钢架`、`桁架`、`拱`、`unknown`。
- `钢架` 包括刚架、框架、门架、闭口框架、组合结构。
- 结构类型筛选只用于字母库图片检索。
- 结构类型优先从题干文字推断；题干没有明确类型时，才调用结构图视觉分类。
- 主库/数值库暂不加结构类型筛选；主库常见重复组候选数不大，加一次结构识别通常不划算。

## Chapter Recognition And Logging

- 自动章节阈值：`AUTO_CHAPTER_MIN_CONFIDENCE = 0.45`。
- Qwen 输出 `unknown` 时仍需要用户选择章节；不要把 `unknown` 当低阈值章节。
- 2/3 章允许典型题型文字推断，例如桁架指定杆轴力、静定结构位移、图乘法、单位荷载法、EA 为常数。
- 4/5/6/7/8 章仍主要依赖明确方法词或强方法步骤证据。
- 飞书章节日志现在是全量判断日志，写入 `data/feishu_chapter_failure_log.jsonl`。
- 该文件名沿用早期“失败日志”命名，但内容包括自动采用、需要手动、手动补章节等样本。
- 日志用于以后训练/收紧章节 prompt；普通代码提交不要默认提交这个运行日志。

## Feishu Behavior

- 飞书机器人默认自动章节模式。
- 用户可发送 `a` 切换自动/手动章节模式；也可发送 `手动`/`m`、`自动`/`auto`。
- 收到图片并拿到 `image_key` 后会给原消息添加 `OK` reaction，避免用户以为机器人没响应。
- 单题检索：收图 -> 章节判断 -> 检索 -> 候选页 -> 回复序号取答案。
- 多题图：先返回题号、章节、荷载摘要；用户回复题号后再逐题检索。
- 多题裁图：OpenCV 找结构图块，能本地绑定就不调用 Qwen；不确定时才用一次 Qwen block-filter。
- 候选页支持删除错题：回复对应负数，如 `-1`、`-2`；删除前必须二次确认。
- 入库模式：`+` 进入，`0` 取消，确认后写入题目图、答案图和 Excel，写前备份。
- 修改飞书逻辑后，正在运行的飞书服务通常需要重启才生效。

## Data Maintenance Rules

- 不要直接按“是否含字母”移动或删除数据；维护主库/字母库拆分时先导出 review，再备份 live Excel，最后用应用脚本写回。
- 删除题库项必须先备份 Excel 和图片文件。
- 自动路径修复会在写 Excel 前备份到 `backups/auto_path_repair_时间戳/`。
- `special_unindexed_questions.json` 记录确认不参与题库检索的特殊题，审计时会排除。
- 题库图片、答案图片和真实配置属于本地资产，不随仓库公开。

## Important Commands

```powershell
python scripts/search_by_loads.py --help
python search.py --help
python scripts/multi_agent_search.py --image "D:\path\to\question.jpg" --chapter auto
python scripts/multi_agent_search.py --types "均布" --raws "q" --chapter "2静定结构" --no-rerank
python scripts/search_by_loads.py loads-search --types "均布" --raws "20" --chapter "2静定结构"
python scripts/search_by_loads.py answer 1
python gui.py
python scripts/feishu_tiku_bot.py dry-run-flow --image "D:\path\to\question.jpg" --chapter 5 --choice 1
python scripts/audit_unindexed_questions.py
python scripts/store_unindexed_questions.py
python scripts/store_unindexed_questions.py --apply
python scripts/smoke_test.py
```

## Verification

- 常规提交前运行：`python scripts/smoke_test.py`。
- smoke test 会检查 live root、答案输出目录、荷载 JSON、图片路径、字母荷载归一化、路径修复、多 Agent 路由、章节 hint、飞书 dry-run 基础状态。
- 如果只改文档，可不跑完整模型调用；但仍建议跑 smoke test 或至少说明未跑原因。

## Known Risks

- `config.json` / `config.local.json` 会覆盖代码默认值，例如 `top_k`；改默认值时要检查有效配置。
- PowerShell `Set-Content -Encoding UTF8` 可能写入 BOM，导致 Python `json.load(..., encoding="utf-8")` 读取配置时报 `Unexpected UTF-8 BOM`；写 JSON 配置时要保持 UTF-8 no BOM。
- 飞书正在运行的旧进程可能还没加载最新代码，修改后需要重启服务。
- 自动章节仍会对纯结构图返回 unknown，这是预期，不要为降低 unknown 率而跨章节猜。
- `data/feishu_chapter_failure_log.jsonl` 可能经常有本地改动，提交前注意排除或单独判断。

## Do Not Do

- 不提交、不展示、不写入 API key、token、密码。
- 不读取完整 `config.json` / `config.local.json` 来展示配置；需要结构时看 `config.example.json`。
- 不回退用户已有改动；遇到未提交改动时只处理与当前任务相关文件。
- 不把全局记忆写入本项目文件。
- 不让 LLM 直接给多题 bbox 做复筛；坐标容易漂，继续用 OpenCV 图块流程。
- 不用 80/90 复筛输出规则改变初筛候选池。

## Next Best Step

- 继续观察飞书章节判断日志，等样本足够后再针对常见 `unknown -> 手动章节` 模式微调章节 prompt/guard。
- 如果检索准确率继续卡住，优先复盘真实失败样本，再决定是否增加新的稳定初筛因素；不要先扩大复杂视觉识别。
- 若继续动检索输出规则，先明确是“初筛候选池”还是“复筛后展示”，避免再把两层逻辑混在一起。
