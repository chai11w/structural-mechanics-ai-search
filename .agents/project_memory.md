# Project Memory

## Current State

- 项目位于 `F:\cc\7-题库检索`。
- 项目目标是结构力学题库检索：按图片或荷载描述检索相似题，并按排名复制答案。
- 当前阶段：准备进入优化阶段。优化前已完成一次项目阅读和项目上下文初始化。
- 当前仓库已有 `.agents/skills/结构力学/` 旧版项目 Skill 说明，根目录也有 `SKILL.md`。
- 项目没有 `README.md`；当前项目说明主要分散在 `SKILL.md`、旧 `.agents/skills/结构力学/` 和代码注释里。
- 当前没有正式测试套件；验证主要依赖 CLI 帮助、脚本运行和人工检索结果检查。
- `config.json`、`config.local.json` 被 `.gitignore` 忽略，可能包含本地路径或 `zhipuai_api_key`。

## Implemented

- `search.py` 是核心 CLI 与业务逻辑：
  - `search`：用图片或荷载 JSON 检索相似题。
  - `store`：把新题图片或路径+荷载写入章节 Excel。
  - `answer`：按上次搜索排名复制答案到输出目录。
- `scripts/search_by_loads.py` 是给 Skill 调用的较薄 CLI 包装：
  - `loads-search`：按 `--types` 和 `--raws` 检索。
  - `image-search`：按图片识别荷载后检索。
  - `answer`：按排名取答案。
- `gui.py` 提供 Tkinter 桌面端，支持图片检索、手动输入荷载、展示结果和打开答案。
- 2026-06-19 GUI 已接入 MVP 复筛：图片检索模式默认先走原荷载粗筛逻辑，再对粗筛候选调用 `rerank_candidates(..., top_n=3)`，最终界面只显示复筛后的 Top 3，并重写 `_last_search.json`，所以“打开答案 1/2/3”对应复筛后的排序。
- GUI 没有新增复筛按钮；手动荷载输入因为没有查询图片可供视觉比较，继续保持原来的荷载粗筛输出逻辑。
- 2026-06-20 GUI 结果区右侧增加“第一名预览”，搜索完成后自动显示当前第一名题图缩略图，减少手动点击“打开图片”确认的次数。
- 2026-06-20 预览区支持鼠标悬停显示左右箭头，可在第 1/2/3 名结果图之间切换预览。
- 2026-06-20 预览图支持点击打开当前正在预览的结果图片，和右侧“打开图片”按钮保持一致。
- 2026-06-23 字母荷载归一化规则第一步已写入 `search.py`：不再把字母固定死，而是按“均布力主题、集中力主题、集中弯矩主题”三个量纲体系编码；长度符号也不固定为 `L`，`a/b/l` 等字母都可作为长度占位。例如任意字母 `A` 在均布/集中/弯矩类型下可分别映射到不同体系，`A/长度、A、A*长度` 表示集中力主题，`A、A*长度、A*长度²` 表示均布力主题，`A/长度²、A/长度、A` 表示集中弯矩主题。同体系按倍数用保留小数编码递增，并继续复用原荷载相似度算法。
- 2026-06-23 增加字母体系冲突消解：同一道题内若某一字母主题体系出现次数占主导，孤立冲突项会在相似度内部归入主导体系。例如 GLM 把真实 `均布:2P/a` 误读成 `均布:2q`，但同题已有 `集中:2P`、`集中:4P`、`弯矩:Pa` 时，`2q` 在相似度计算中按力体系 `0.021` 处理；单独 `均布:2q` 仍保持均布力主题 `0.011`。同时移除旧的“有 ql/qa² 就删除 q”后处理，并增加模型输出类型别名归一化。
- `build_index.py` 用 GLM-5V-Turbo 批量识别题目图片荷载并生成章节 Excel 索引。
- 仓库根目录已有章节索引 Excel：`2静定结构.xlsx`、`3静定结构位移.xlsx`、`4力法.xlsx`、`5位移法.xlsx`、`6力矩分配.xlsx`。

## Current Local Changes

- `build_index.py`、`gui.py`、`search.py` 当前有未提交修改。
- 这些修改的当前含义：
  - `ZHIPUAI_API_KEY` 支持从环境变量读取；环境变量为空时回退到本地配置里的 `zhipuai_api_key`。
  - `search.py` 已禁用搜索后自动打开 Top 结果图片。
  - `search.py store` 在图片未识别到荷载时会取消储存，避免写入空荷载记录。
- `.claude/settings.local.json` 也有本地修改，但不是当前项目上下文维护的目标。

## Optimization Direction From Personal Brain

- 2026-06-19 从 `F:\cc\13khoj第二大脑-记忆` 做了本地只读检索；未调用个人大脑 `ask` 外部模型接口。
- 直接相关记忆证据显示：当前结构力学搜题器还是“返回最相似的若干题目，让用户人工比对”；用户曾提出下一步方向是在匹配后增加 AI 判断层，由 AI 基于相似题目自动推理并直接给出最终答案。
- 更具体的结构力学检索器改进方案：
  - 章节自动选择：让模型根据题目自动判断章节，减少用户手动选章节。
  - 量纲识别 Skill/规则：让模型理解均布荷载 `q (F/L)`、集中荷载 `F`、弯矩/力矩量纲、`ql²`、`FL` 等表达。
  - 当前待修复缺陷：大模型对字母符号识别不足。
  - 候选方案：对字母荷载按“符号体系 + 结构力学量纲 + 倍数”三层归一，再做小数值映射，用于绕开字母符号导致的匹配失败，同时尽量复用现有数值检索算法。
    - 均布荷载常见表达：`q`、`F/L`。
    - 集中力常见表达：`F`、`qL`。
    - 集中弯矩常见表达：`M`、`FL`、`ql²`。
    - `F` 体系应整体保留为一套：`F/L`（均布）、`F`（集中力）、`FL`（集中弯矩）。
    - `q` 体系应整体保留为一套：`q`（均布）、`qL`（集中力）、`ql²`（集中弯矩）。
    - `M` 体系也应保留为一套，虽然不常见：`M/L²`（均布）、`M/L`（集中力）、`M`（集中弯矩）。
    - 映射不能只按倍数把 `F`、`qL`、`M`、`FL`、`ql²` 全压成同一个 `0.01`，否则会丢失符号体系关系，导致跨类型组合检索混乱。
    - 更合理的第一版是为不同符号体系预留不同小数区间；同一体系内再按倍数递增。例如 `F` 体系 1 倍用一个保留值，`q` 体系 1 倍用另一个保留值。具体数值需通过样例验证后确定。
  - 两级排序架构：先用现有 RAG/荷载相似度初筛，再用 LLM 根据支座类型等结构特征二次筛选和重排。
  - 目标输出：从候选列表升级为更少、更准的 Top 3，长期可进一步直接给出答案。
- 可借鉴的小柴检索原则：不要只固定返回 Top N；可以引入相似度阈值，用阈值控制结果质量。
- 可复用工作方法：先梳理现有完整流程，再判断 AI 应该插入哪一步，明确预期效果和验证方式。

## Support-Aware Rerank Plan

- 2026-06-19 用户提出：只靠荷载相似度粗筛不够，后续应引入支座信息和 LLM 精排，但先记录方案，暂不实现。
- 支座类型第一版只使用五类：
  - `固定端`
  - `固定铰支座`
  - `可动铰支座`
  - `滑动支座`
  - `自由端`
- `刚节点` 不属于支座，不能混入支座分类。
- 第一版支座相似度应复用现有荷载相似度思路，只看类型计数，不先看位置：
  - 查询题和候选题各自得到支座类型列表。
  - 按类型计数求交集。
  - 相似度可先用 `交集数量 / max(查询支座总数, 候选支座总数)`。
  - 例：候选题 `固定端, 固定端, 滑动支座`，查询题 `固定端`，相似度为 `1/3`。
- 粗筛阶段负责稳定、可重复的结构化因素：
  - 荷载类型/数量相似度。
  - 支座类型/数量相似度。
  - 可以先用综合分：`荷载相似度 * 0.7 + 支座相似度 * 0.3`，具体权重需实验验证。
  - 粗筛候选不宜只取 5 个，建议先取 Top 10 或 Top 15，避免正确题被过早过滤。
- LLM 精排阶段不再重复判断荷载数量和支座数量，因为这些已由结构化粗筛覆盖。
- LLM 精排只看规则算法难覆盖的视觉因素：
  - 结构形状是否相近。
- 2026-06-19 已实现一个可选 MVP：`--rerank`。它不改变默认搜索；只在图片搜索显式加 `--rerank` 时启用。
  - 原有荷载粗筛逻辑保持不变：默认 Top 5；若 100% 匹配超过 5 个，则全部进入候选。
  - 复筛阶段只在粗筛候选内工作，最终输出 Top 3，并更新 `_last_search.json`，因此 `answer 1/2/3` 对应复筛后的排名。
  - 第一次尝试“查询图 + 所有候选图一次性排序”失败：查询图本身在候选中却没有排第一，说明多图排序不可靠。
  - 已改为“逐候选打分”：每次只比较查询图和一个候选图，按 `结构形状` 给分，再由代码排序。
  - 2026-06-20 复筛条件进一步收窄：不再判断荷载位置，只用结构形状做视觉复筛。
  - 2026-06-19 增加复筛阈值：荷载相似度低于 50% 的候选不进入 LLM 复筛；过滤后不足 3 个时，只输出过滤后的复筛结果，不用低质量候选硬补 Top 3。
  - 2026-06-20 复筛排序改为最终相似度；当前权重为荷载粗筛分 50% + 视觉复筛分 50%。用户可见搜索结果只显示最终相似度，不再同时展示荷载相似度、复筛分和计算过程。
  - 2026-06-20 增加 100% 打平复核：如果最终相似度 100% 的候选超过 1 个，才额外调用 LLM 比较杆件长度、跨长、高度和整体比例，并用该分数拉开并列结果。
  - 样本验证：对 `2静定结构/3钢架/1内力图/题目2/3铰/50.jpg` 搜索并复筛时，同图候选从荷载粗筛第 5 名升到复筛第 1 名。
- 推荐最终流程：
  1. 识别查询图荷载列表。
  2. 识别查询图支座类型列表。
  3. 用 Excel 中候选题的荷载 + 支座结构化信息算综合相似度。
  4. 取 Top 10/15 候选。
  5. LLM 只对候选做精排，重点比较结构形状。
  6. 输出最终 Top 3，并给简短理由。
- 该方案还未实现。下一步应先做支座识别小样本评测，再决定是否把 `支座` 列加入实验副本 Excel。

## Letter Load Evaluation

- 2026-06-19 已从 `2静定结构` 选 5 张历史上带字母荷载的图片调用项目 GLM-5V-Turbo 评测，结果保存在 `.tmp_eval_5_letter_images.md`。
- 期望 vs 实际：
  - `ql`：期望 `集中:ql`；模型输出 `集中:ql`，但额外输出 `均布:q`。
  - `2P/4P/Pa`：期望 `集中:2P`、`集中:4P`、`弯矩:Pa`；模型全部识别到，但额外输出 `均布:2q`。
  - 多个 `m` 弯矩：期望 5 个 `弯矩:m`；模型输出 6 个 `弯矩:m`。
  - `q`：期望 `均布:q`；模型输出正确。
  - `qa²`：期望 `弯矩:qa²`；模型输出正确，但额外输出 `均布:q`。
- 初步判断：当前模型并非完全不会识别字母荷载，主要问题是容易额外识别符号荷载或计数偏多。第一版优化应包含后处理/过滤策略，不只是 prompt。
- 2026-06-19 曾在项目目录 Excel 副本上试清理非具体赋值符号荷载项，并备份到 `backups/excel_before_symbol_cleanup_20260619_133926`；随后已从备份恢复项目目录 Excel。当前正式运行使用 `D:\桌面\答疑、帮做\结构力学\帮做` 下的主题库，未被这次清理实验修改。
- 后续题库优化必须先复制到独立实验目录验证；成功后再由用户确认是否合并到主库。
- 2026-06-19 已在 `search.py` 做第一版识别优化：
  - prompt 增加符号荷载规则，要求 `ql`、`qa²`、`Pa`、`FL` 等复合符号作为整体提取，不拆出 `q/P`。
  - 增加很窄的后处理：当结果中已有 `ql/qa/ql²/qa²` 等 q 复合表达时，删除同次结果里额外拆出的 `均布:q`。
  - 本地回放旧 5 样本结果时，A/E 的额外 `q` 能被删除，`2P/4P/Pa` 不会被误删。
  - 真实 API 复测已完成：5 张样本中 A/C/D/E 与期望一致；B 的 `2P`、`4P`、`Pa` 都识别正确，但仍额外输出 `均布:2q`。
  - 当前结论：第一版 prompt + 窄后处理已明显改善复合 q 符号拆分问题；剩余主要问题是个别图片会把疑似非荷载符号误识别为 `q` 系均布荷载。不要用过宽后处理直接删除所有 `q`，因为真实题可能同时有 P 系与 q 系荷载。

## Not Implemented

- 没有自动化测试或固定样例回归脚本。
- 没有统一依赖文件；代码注释中只提到需要 `zhipuai pandas openpyxl`，GUI 拖拽可能需要 `tkinterdnd2`。
- 顶层 `SKILL.md` 的命令示例仍像旧接口，和当前 `scripts/search_by_loads.py` 的子命令接口不完全一致。

## Architecture Rules

- 检索流程应通过项目脚本完成，不用额外视觉工具判图或二次比较搜索结果。
- 章节边界必须由用户指定或确认，不要自动扩大搜索章节。
- 荷载结构统一为 `{"loads": [{"type": "...", "raw": "..."}]}`；类型只允许 `集中`、`均布`、`弯矩`。
- 本地配置读取顺序是项目目录下 `config.json` 再叠加 `config.local.json`；环境变量 `ZHIPUAI_API_KEY` 优先于配置文件中的 key。
- `search.py` 和 `build_index.py` 都设置 `no_proxy` / `NO_PROXY` 以绕过 Windows 系统代理。

## Important Commands

- 运行只读 smoke test：
  ```powershell
  python scripts/smoke_test.py
  ```
- 查看薄包装 CLI 帮助：
  ```powershell
  python scripts/search_by_loads.py --help
  ```
- 按荷载检索：
  ```powershell
  python scripts/search_by_loads.py loads-search --types "均布" --raws "20kN/m" --chapter "2静定结构"
  ```
- 多荷载检索：
  ```powershell
  python scripts/search_by_loads.py loads-search --types "集中" "弯矩" --raws "10kN" "10kN·m" --chapter "2静定结构"
  ```
- 按图片检索：
  ```powershell
  python scripts/search_by_loads.py image-search --image "D:\path\to\img.jpg" --chapter "2静定结构"
  ```
- 取答案：
  ```powershell
  python scripts/search_by_loads.py answer 1
  ```
- 使用核心 CLI 搜索：
  ```powershell
  python search.py search --loads '{"loads":[{"type":"集中","raw":"10kN"}]}' --chapter "2静定结构"
  ```
- 启动 GUI：
  ```powershell
  python gui.py
  ```

## Known Risks

- 顶层 `SKILL.md` 的示例命令可能过期：当前 `scripts/search_by_loads.py` 使用 `loads-search`、`image-search`、`answer` 子命令。
- 旧 `.agents/skills/结构力学/references/*.md` 中的绝对路径指向 `D:\桌面\答疑、帮做\结构力学\帮做\search.py`，可能不匹配当前仓库路径 `F:\cc\7-题库检索`。
- 旧 `.agents/skills/结构力学/references/retrieval.md` 仍说脚本会自动打开匹配图片；这和当前 `search.py` 已禁用自动弹图的行为冲突。
- `python scripts/smoke_test.py` 当前通过，但有 1 个警告：`4力法` 前 20 行样本里 `4力法/2钢架/1单未知量/题目1/13.jpg` 和 `4力法/2钢架/1单未知量/题目1/18.jpg` 图片路径缺失。
- `answer()` 会清空并重建答案输出目录；调用前确认 `answer_output` 配置正确。
- 图片识别依赖 GLM-5V-Turbo 和有效 `ZHIPUAI_API_KEY`。
- 当前工作树已有未提交/未跟踪改动，修改前要先确认相关文件现状。

## Do Not Do

- 不把 API key、token、密码写入项目上下文、日志或提交。
- 不读取或整理全局记忆作为本项目上下文的一部分。
- 不绕开脚本直接看图判断相似题。
- 不在用户指定单章节时跨章节搜索。
- 不随意回退 `.claude/settings.local.json`、`build_index.py`、`gui.py`、`search.py` 等已有改动。

## Next Best Step

- 优先同步顶层 `SKILL.md` 和旧 `.agents/skills/结构力学/references/*.md`，让命令、路径和“是否自动打开图片”的描述与当前代码一致。
- 之后考虑补一个最小 smoke test：验证 `scripts/search_by_loads.py --help`、`search.py --help` 和纯荷载检索样例不会崩。
- 优化时先处理“文档/Skill 与代码不一致”这一类低风险高收益问题，再动检索相似度、GUI 交互或索引逻辑。
- 进入功能优化时，推荐顺序是：修文档一致性和 smoke test -> 量纲/字母荷载识别规则 -> 相似度阈值与 Top 3 输出 -> 支座类型二次筛选 -> AI 直接答案层。
- 字母荷载归一化要先做小样本验证：确认 `F` 体系（`F/L`、`F`、`FL`）、`q` 体系（`q`、`qL`、`ql²`）和少见的 `M` 体系（`M/L²`、`M/L`、`M`）能被稳定区分，同时各自仍能按均布/集中/弯矩分到正确荷载类型，并且不会和真实小数数值混淆。
- 当前最重要的主线护栏已建立：`scripts/smoke_test.py`。下一步建议先处理 smoke test 报出的 `4力法` 图片路径缺失，再继续模型识别或相似度优化。
- 2026-06-20 荷载 raw 归一化兼容裸 `k` 单位：数值荷载里的 `20k`、`20kN`、`20kN/m` 等都按 `20` 比较，因为当前题库默认使用 kN 体系。
## 2026-06-23 Handoff: Symbolic Bank Split

- Current live question bank Excel files are the files under configured `ROOT`: `D:\桌面\答疑、帮做\结构力学\帮做\*.xlsx`. These are the indexes the GUI/CLI actually read. The repo-root `*.xlsx` copies may mirror them, but the live source for the software is `ROOT`.
- Qwen3.7-Plus was tested on symbolic-load images through DashScope. First 10 manually checked samples were 10/10 correct. A later 30-image run found 21 exact matches with existing Excel after simple format normalization; the 9 differences were mostly old Excel omissions or format differences, not clear Qwen visual failures.
- Important correction from user: if problem text assigns symbol values, e.g. `P=40kN`, `q=20kN/m`, `F1=40kN`, the item must be treated as a concrete numeric-load problem, not a pure symbolic-load problem. `search.py` now prompts for assignment-aware extraction and normalizes `F1=40kN -> 40`, `F2=2ql -> 2ql` for similarity.
- Current decision direction: split the question bank into two tracks:
  - Main/numeric bank: concrete loads and assigned-symbol loads such as `P=40kN`, `q=20kN/m`, `F1=40kN`.
  - Symbolic experimental bank: unassigned symbolic loads such as `q`, `ql`, `2P/a`, `M`, `qa²`, `Fp`, etc.
- Recommended next step before any deletion: export a review sheet of all live Excel rows whose loads are unassigned symbolic expressions, copy those rows into separate symbolic-bank Excel files, and back up the original live Excel files. Only after the export and review should those pure symbolic rows be removed from the main/numeric bank.
- Do not blindly delete all rows containing letters. Rows with explicit assignments (`P=40kN`, `q=20kN/m`, `F1=40kN`, etc.) belong in the main/numeric bank because similarity can resolve them to concrete values.
- Recent pushed commit for assignment handling: `9565d85 Handle assigned symbolic loads`. Smoke test passed after this change: `python scripts/smoke_test.py` -> `SUMMARY PASS warnings=0`.

## 2026-06-23 Main/Symbolic Bank Update Applied

- Live main bank backup before writing:
  - `F:\cc\7-题库检索\backups\live_excel_before_main_update_20260623_134752`
- Qwen3.7-Plus via DashScope was used with `enable_thinking=false`; enabling thinking caused remote disconnects on some symbolic images. The classification script sends upscaled images and records per-image errors as `needs_review` instead of stopping the batch.
- Full image classification completed:
  - Existing main-index images classified: 485.
  - Missing-from-main images classified: 214.
  - Existing main candidates: 369 `main_numeric`, 38 `main_assigned_symbolic`, 70 `symbolic_unassigned`, 6 `needs_review`, 2 `mixed_symbolic_numeric`.
  - Missing candidates: 184 `symbolic_unassigned`, 16 `main_numeric`, 8 `main_assigned_symbolic`, 6 `needs_review`.
- Live main Excel update was applied after backup and review:
  - Deleted 70 pure unassigned symbolic rows from main bank.
  - Appended 24 missing main-bank rows (numeric or assigned-symbolic).
  - Left all `needs_review` and `mixed_symbolic_numeric` rows untouched.
  - Post-update live main row counts: `2静定结构` 201, `3静定结构位移` 31, `4力法` 72, `5位移法` 57, `6力矩分配` 80.
  - Application report: `.tmp_symbol_sheets/main_update_applied/main_update_summary.md`.
- Independent symbolic bank was generated at:
  - `D:\桌面\答疑、帮做\结构力学\帮做_字母库`
  - It mirrors the main bank format: one same-name chapter Excel per chapter, columns `题目名称` and `荷载`.
  - Symbolic-bank row counts: `2静定结构` 90, `3静定结构位移` 26, `4力法` 82, `5位移法` 49, `6力矩分配` 7; total 254.
- Important symbolic-bank write rule:
  - `raw` is the similarity code, not the original letter text.
  - `original_raw` stores the original visual label for review.
  - Example: `2P/a` becomes `{"type":"均布","raw":"0.021","original_raw":"2P/a"}`.
  - Similarity computation reads only `type` and `raw`; `original_raw` is metadata and should not affect search scoring.
- Symbolic encoding rules used:
  - Distributed-load family base `0.010`: `q`, `ql/qL/qa`, `ql²/qa²`; coefficient code is `base + (factor - 1) * 0.001`.
  - Force family base `0.020`: `F/P/Fp`, `F/L/P/a/2P/a`, `FL/Pa/FpL`; coefficient code uses the same formula.
  - Moment family base `0.030`: `M/m`, `M/L`, `M/L²`; coefficient code uses the same formula.
  - `P/F/Fp/F_P/F_p` share the force family; length symbols may be `L/a/b/l`; `F1=2ql` is symbolic and maps by the right-hand side, while `P=40kN` remains main-bank numeric/assigned.
- New maintenance scripts:
  - `scripts/classify_question_bank.py`: scans images and classifies records with Qwen; writes review JSON/XLSX/contact sheet; does not edit live Excel.
  - `scripts/apply_main_bank_update.py`: after review and backup, removes pure-symbolic rows from main Excel and appends missing main-bank rows. It expands assigned symbols on append, e.g. `P=40kN` with `2P` writes a concrete `80kN` load; `FP=28kN` with `FP/2` writes `14kN`.
  - `scripts/build_symbolic_bank.py`: builds the separate same-format symbolic bank and writes encoded `raw` plus `original_raw`.
- `search.py` now supports symbol divided by numeric coefficient, e.g. `P/2` and `FP/2` map to force-family `0.0195`.
- Verification after writing:
  - Formal symbolic bank validation: every row has valid JSON, existing image path, code-like `raw`, and `original_raw`.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.

## 2026-06-23 Live Excel Path Repair Rule

- When a live Excel `题目名称` path no longer exists, do not immediately treat the row as invalid. First recursively search under the same chapter/topic question folder, because the user may have moved the image into a newly created subfolder.
- If exactly matching content/name is found in a new location, update the Excel `题目名称` to the new relative path. Also normalize path casing to match the actual file name, because the classifier/review scripts use exact path strings for cross-checking.
- If path normalization creates an exact duplicate row with the same `题目名称` and `荷载`, keep one row and remove the duplicate after backing up the live Excel.
- Applied repair:
  - Backup: `F:\cc\7-题库检索\backups\live_excel_before_path_repair_20260623_141649`
  - `4力法.xlsx`: changed `4力法/2钢架/1单未知量/题目1/12.jpg` to `4力法/2钢架/1单未知量/题目1/0/12.jpg`.
  - `4力法.xlsx`: changed `4力法/2钢架/1单未知量/题目1/51.JPG` to `4力法/2钢架/1单未知量/题目1/51.jpg`, then removed the duplicate `51.jpg` row with the same load.
  - After repair, `4力法.xlsx` has 71 data rows; full-row path check for this workbook found 0 missing paths and 0 case mismatches.
  - The same backup folder also includes `3静定结构位移.xlsx`; row 30 had Chinese commas inside the JSON load cell and was repaired to valid JSON without changing the load values.
  - `scripts/smoke_test.py` was strengthened from first-20-row sampling to full-table checks for load JSON validity, image existence, and exact path casing.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.

## 2026-06-23 Experimental Multi-Agent Retrieval

- First CLI-only multi-agent retrieval layer was added; it does not replace the existing GUI flow yet.
- New files:
  - `multi_agent_pipeline.py`: defines `QwenClassifier`, `RuleRouter`, `MultiAgentCoordinator`, and shared formatting/search helpers.
  - `scripts/multi_agent_search.py`: CLI entry for the experimental pipeline.
- Pipeline design:
  - Qwen/DashScope is the high-accuracy front classifier for image load extraction and bank category detection.
  - The local rule router maps loads to `main`, `symbolic`, or `needs_review`.
  - Main bank is used for numeric and assigned-symbol loads; symbolic bank is used for unassigned symbolic loads; mixed/empty/unknown loads are not searched automatically.
  - Zhipu keeps the existing low-latency visual rerank role for Top candidates.
  - Qwen image classification results are cached under `.tmp_multi_agent/qwen_classifier_cache.json` by image hash and model.
- CLI examples:
  - `python scripts/multi_agent_search.py --image "D:\path\to\question.jpg" --chapter "2静定结构"`
  - `python scripts/multi_agent_search.py --types "均布" --raws "q" --chapter "2静定结构" --no-rerank`
- Verification:
  - `python scripts/smoke_test.py` now checks multi-agent route rules.
  - Local no-network CLI checks confirmed numeric loads route to main bank, symbolic `q` routes to symbolic bank, and mixed `q + 10kN` routes to `needs_review`.
