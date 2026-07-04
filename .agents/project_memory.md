# Project Memory

## Current State

- 项目位于 `F:\cc\7-题库检索`。
- 项目目标是结构力学题库检索：按图片或荷载描述检索相似题，并按排名复制答案。
- 当前阶段：准备进入优化阶段。优化前已完成一次项目阅读和项目上下文初始化。
- 当前仓库已有 `.agents/skills/结构力学/` 旧版项目 Skill 说明，根目录也有 `SKILL.md`。
- 项目没有 `README.md`；当前项目说明主要分散在 `SKILL.md`、旧 `.agents/skills/结构力学/` 和代码注释里。
- 当前没有正式测试套件；验证主要依赖 CLI 帮助、脚本运行和人工检索结果检查。
- `config.json`、`config.local.json` 被 `.gitignore` 忽略，可能包含本地路径或 `zhipuai_api_key`。

## Standing Collaboration Rule

- 用户要求：本项目以后每次完成代码或题库逻辑更改，都要同步写入 `.agents/project_memory.md`，并提交、推送到 GitHub，方便换对话后继续接上最新状态。
- 常规收尾顺序：更新项目记忆 -> 运行相关验证 -> `git commit` -> `git push`。

## 2026-07-04 Chapter Judgment Logging Phase

- User wants automatic chapter recognition to move from the current conservative 3-4/10 recall toward 8/10+ without making retrieval slower or introducing broad cross-chapter mistakes.
- Decision: do not loosen the chapter prompt immediately. First collect real judgment data from existing and future usage, then tune prompt/rules against observed false-unknown and false-positive cases.
- Added append-only log helper `scripts/chapter_judgment_log.py`.
- Future chapter judgment events write JSONL records to `data/chapter_judgment_log.jsonl`, including:
  - source entry point such as `gui_image_search`, `feishu_image_search`, `feishu_store_classify`, `feishu_store_manual_chapter`, `cli_image_search`;
  - image path;
  - requested chapter and final chapter;
  - decision mode `auto`, `manual`, `needs_manual`, or `manual_path`;
  - Qwen `chapter_hint`, `chapter_confidence`, `chapter_evidence`;
  - loads, route/category, result count, rerank flag where available.
- Integrated logging into:
  - `MultiAgentCoordinator.search_image/search_loads` for GUI/Feishu/CLI retrieval;
  - Feishu store classification and manual store chapter selection;
  - GUI legacy store button.
- Logging intentionally skips records without a real image path and skips mock test images so smoke tests do not pollute the dataset.
- Exported current live Excel chapter labels with `scripts/export_chapter_judgment_seed.py`; output `data/chapter_judgment_seed.jsonl` currently has `764` existing labeled records from main and symbolic workbooks. This seed is a path/chapter label baseline, not model OCR evidence.
- A real CLI image search on `8影响线/2数值计算/题目a/2.jpg` wrote a sample log record with `chapter_hint=8影响线`, `final_chapter=8影响线`.
- Verification:
  - `python scripts/export_chapter_judgment_seed.py` reported `seed_records=764`.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.
- Next prompt-tuning threshold: after enough real manual correction samples accumulate, inspect logs for common `unknown -> final_chapter` patterns and only then relax prompt/rules with a small evaluation set.

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
  - 2026-06-23 更新：如果最终进入复筛的候选数不超过 3 个，直接输出粗筛结果，不再调用 Zhipu 复筛，以减少低收益模型调用。
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

- Multi-agent retrieval layer was added and then wired into GUI image search as the default image workflow.
- New files:
  - `multi_agent_pipeline.py`: defines `QwenClassifier`, `RuleRouter`, `MultiAgentCoordinator`, and shared formatting/search helpers.
  - `scripts/multi_agent_search.py`: CLI entry for the experimental pipeline.
- Pipeline design:
  - Qwen/DashScope is the high-accuracy front classifier for image load extraction and bank category detection.
  - The local rule router maps loads to `main`, `symbolic`, or `needs_review`.
  - Main bank is used for numeric and assigned-symbol loads; symbolic bank is used for unassigned symbolic loads; mixed/empty/unknown loads are not searched automatically.
  - Zhipu keeps the existing low-latency visual rerank role for Top candidates.
  - Qwen image classification results are cached under `.tmp_multi_agent/qwen_classifier_cache.json` by image hash and model.
- GUI behavior:
  - Image search now calls `MultiAgentCoordinator.search_image(...)` and therefore defaults to Qwen classification + bank routing + Zhipu rerank.
  - Manual load search calls the same coordinator route/search path, but does not rerank because there is no query image for visual comparison.
  - Manual numeric loads may omit units. The query normalizer fills default units by type: `集中 -> kN`, `均布 -> kN/m`, `弯矩 -> kN·m`; symbolic raws such as `q`, `2P/a`, `F1=2ql` are left unchanged.
  - `DASHSCOPE_API_KEY` can be read from the environment or from local config key `dashscope_api_key`; do not commit that key.
- CLI examples:
  - `python scripts/multi_agent_search.py --image "D:\path\to\question.jpg" --chapter "2静定结构"`
  - `python scripts/multi_agent_search.py --types "均布" --raws "q" --chapter "2静定结构" --no-rerank`
- Verification:
  - `python scripts/smoke_test.py` now checks multi-agent route rules.
  - Local no-network CLI checks confirmed numeric loads route to main bank, symbolic `q` routes to symbolic bank, and mixed `q + 10kN` routes to `needs_review`.
- MVP rerank-pool rule:
  - Main bank non-perfect candidates enter Zhipu rerank only when score is at least `65%`.
  - Symbolic bank non-perfect candidates enter Zhipu rerank when score is at least `50%`.
  - Perfect `100%` candidates always enter the rerank candidate pool for both banks; do not cap them yet, because recall is more important at this stage.
  - If the final rerank candidate pool has 3 or fewer candidates, skip Zhipu and return the coarse-search result directly.

## 2026-06-23 Feishu Tiku Bot MVP

- Direction decision:
  - Do not directly reuse/couple to `F:\cc\13khoj第二大脑-记忆`; that project is reference only for Feishu event/token patterns.
  - Build a project-native, dedicated structure-mechanics tiku bot first. Do not generalize into a "lobster platform" until this flow works.
- New file:
  - `scripts/feishu_tiku_bot.py`: local Feishu event shell + tiku session state machine.
- Current implemented flow:
  - Receive image -> store session -> ask chapter.
  - Chapter replies use chapter numbers `2/3/4/5/6`, not list indices, to avoid confusing `5` with the fifth menu item.
  - Reply `0` in any waiting state cancels/exits the current tiku session.
  - Search calls `MultiAgentCoordinator.search_image(..., rerank=True, rerank_top=3)`.
  - Sends Top 3 candidate image paths in dry-run; waits for `0/1/2/3`, where `0` means no desired match/exit.
  - Choice calls `answer(rank)` in real mode. In dry-run it does not write answer output and only echoes the selected candidate image.
- Dry-run command:
  - `python scripts/feishu_tiku_bot.py dry-run-flow --image "D:\path\to\question.jpg" --chapter 5 --choice 1`
  - Add `--real-search` only when intentionally calling Qwen/Zhipu/local live search from the dry-run command.
- Feishu integration state:
  - Text event shell, URL verification, token cache, stale-message filtering, image upload/download method boundaries are present.
  - User configured dedicated Feishu credentials as Windows user-level env vars: `FEISHU_TIKU_APP_ID`, `FEISHU_TIKU_APP_SECRET`, `FEISHU_TIKU_VERIFICATION_TOKEN`.
  - `feishu_tiku_bot.py` falls back to reading Windows user-level env vars via registry if the current process environment has not inherited them.
  - Real Feishu image download/upload has not been live-tested yet; next step is send a real Feishu image message after pasting the latest tunnel URL into Feishu event subscription.
- One-click runtime:
  - `启动结构力学题库.bat` calls `scripts/start_tiku_bot.ps1`.
  - `scripts/start_tiku_bot.ps1` starts hidden `scripts/tiku_bot_watchdog.ps1`; it stops only this project's recorded PID files and port 8788, not Xiaochai/second-brain processes.
  - `scripts/tiku_bot_watchdog.ps1` starts and health-checks `scripts/feishu_tiku_bot.py --port 8788`, starts this project's own `cloudflared tunnel --url http://127.0.0.1:8788`, and writes the current event URL to `.tmp_feishu_tiku/feishu_tiku_latest_url.txt`.
- Verification:
  - `python scripts/smoke_test.py` now checks the Feishu tiku bot dry-run state machine.
  - On 2026-06-23, local `http://127.0.0.1:8788/health` returned `{"ok": true}`.
  - The temporary trycloudflare `/health` URL also returned `{"ok": true}`.
  - A local Feishu `url_verification` payload using the configured token returned the expected challenge.

## 2026-06-24 Chapter Hint Extraction MVP

- Direction: reduce manual chapter selection, but do not silently guess chapters. The first step is only to add chapter suggestion fields to the existing Qwen image classification result; GUI/Feishu automatic chapter usage is not wired yet.
- `scripts/classify_question_bank.py` Qwen prompt now asks for:
  - `chapter_hint`: one of `2静定结构`, `3静定结构位移`, `4力法`, `5位移法`, `6力矩分配`, `unknown`.
  - `chapter_confidence`: 0-1 float.
  - `chapter_evidence`: visible text/method evidence for the decision.
- Important guardrail from sample testing:
  - Qwen initially guessed `4力法/1梁/1单未知量/题目/2跨/11.jpg` as `6力矩分配` just because the image looked like a continuous beam with EI. This is unsafe.
  - Prompt was tightened: for `4力法`, `5位移法`, and `6力矩分配`, do not infer from continuous beams, EI/stiffness, support count, or hyperstatic appearance; require explicit method text.
  - Qwen also guessed `4力法/6超静定位移计算/题目/3.jpg` as `3静定结构位移` because it saw “求 B 点的转角” and hallucinated graph multiplication. Therefore “求位移/求转角” alone must not imply `3静定结构位移`.
- Programmatic guardrail:
  - `guard_chapter_prediction()` only accepts a non-unknown chapter hint when `chapter_evidence` contains quoted visible text with chapter trigger words.
  - Accepted trigger examples: quoted `图乘法` or `静定结构位移` for chapter 3, quoted `力法` for chapter 4, quoted `位移法`/`转角位移` for chapter 5, quoted `力矩分配`/`弯矩分配` for chapter 6.
  - Otherwise the result is downgraded to `unknown` with confidence capped at `0.49`.
- `multi_agent_pipeline.QwenClassifier.classify_image()` now carries `chapter_hint`, `chapter_confidence`, and `chapter_evidence` through cache/results, while keeping old cache entries backward-compatible.
- Verification:
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.
  - Qwen sample results after guardrail:
    - `3静定结构位移/1梁/题目a/1跨/12.jpg` -> `3静定结构位移` because evidence quotes `图乘法`.
    - `4力法/6超静定位移计算/题目/3.jpg` -> downgraded to `unknown` because only “求 B 点的转角” is explicit.
    - `6力矩分配/2多节点分配/不可简化/题目aa/6.jpg` -> `6力矩分配` because evidence quotes `弯矩分配法`.
- Next step:
  - Wire `chapter=auto` into `MultiAgentCoordinator` and CLI/GUI/Feishu only after deciding the confidence threshold. Recommended first threshold: accept only non-unknown hints with confidence `>=0.8`, otherwise ask the user to choose chapter.

## 2026-06-24 Auto Chapter Coordinator MVP

- `MultiAgentCoordinator` now supports `chapter=None` or `chapter="auto"` for image/manual-load pipeline calls.
- Qwen cache key now includes schema version `chapter-v1`, so pre-chapter cached classifications are not reused for auto chapter detection.
- Auto chapter rule:
  - If a concrete chapter is passed, it always wins. Do not override user-selected chapters.
  - If chapter is `auto`, only use Qwen `chapter_hint` when it is not `unknown` and `chapter_confidence >= 0.8`.
  - If auto chapter is missing or low confidence, return route `needs_chapter` with no search results. Do not search across all chapters.
- `PipelineResult` now carries chapter metadata:
  - `chapter`: actual chapter used, or `None`.
  - `chapter_hint`, `chapter_confidence`, `chapter_evidence`.
- `scripts/multi_agent_search.py` now has `--chapter auto` as the default. Image search can use it; manual `--loads/--types` with `auto` returns `needs_chapter` because there is no Qwen image classification to infer chapter from.
- Exit codes:
  - `0`: searched normally.
  - `3`: `needs_review`.
  - `4`: `needs_chapter`.
- Verification:
  - `scripts/smoke_test.py` checks manual-chapter priority, high/low confidence auto chapter behavior, unknown rejection, and manual-load `auto` fallback to `needs_chapter`.
  - Real CLI check: `5位移法/1梁/1单未知量/题目/1.jpg` with `--chapter auto --no-rerank` auto-selected `5位移法` and searched successfully.
  - Real CLI check: `2静定结构/1单跨梁/题目a/1力/13.jpg` with `--chapter auto --no-rerank` returned `needs_chapter` and exit code `4`.
- Next step:
  - Wire this into Feishu first: after receiving an image, call image search with `chapter="auto"`. If route is `needs_chapter`, ask the user for 2/3/4/5/6; otherwise skip the chapter question and return candidates.
  - GUI can be wired after Feishu by adding an “自动识别章节” option in the chapter dropdown.

## 2026-06-24 Feishu Auto Chapter Mode

- Feishu tiku bot now defaults each sender to auto chapter mode.
- Auto mode behavior:
  - User sends image.
  - Bot calls `MultiAgentCoordinator.search_image(..., chapter="auto")`.
  - If auto chapter succeeds, bot immediately returns Top candidates and includes `已自动识别章节：...`.
  - If route is `needs_chapter`, bot saves the image session and asks for chapter `2/3/4/5/6`.
- Manual fallback:
  - User can send `a` to toggle that sender between auto chapter mode and manual chapter mode.
  - User can send `手动`, `manual`, or `m` to explicitly switch to manual chapter mode.
  - User can send `自动` or `auto` to explicitly switch back to auto chapter mode.
  - Switching modes clears the current search session; user should re-upload the image after switching.
- If auto-selected chapter is not desired, the intended user flow is:
  - Reply `a` to switch to manual mode.
  - Re-upload the same题图.
  - Choose chapter number manually.
- Existing `0` cancel behavior remains unchanged in waiting states and candidate-choice states.
- Candidate reply format was shortened:
  - First line: `章节：...`
  - Second line: `下面是相似题目 Top N，相似比分别为：...`
  - Third line: `0：结束`
  - Fourth line: `a：切换手动识别章节`
  - Do not repeat `已自动识别章节` and `已检索`; the concise chapter line is enough.
- Image receive acknowledgement:
  - After Feishu image messages provide an `image_key`, the bridge immediately adds an `OK` reaction to the original image message before downloading/running Qwen/Zhipu, so the user can see the bot is working during slow model calls.
  - `--working-reaction` controls the Feishu `emoji_type`; default is `OK`, empty disables it.
- `scripts/feishu_tiku_bot.py dry-run-flow` preserves the old manual flow for local dry-run testing by setting the dry-run sender to manual mode before sending the mock image.
- Verification:
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.
  - Dry-run command verified image -> chapter -> candidates -> `0` cancel flow still works.

## 2026-06-24 GUI Auto Chapter Selection

- GUI chapter combobox now includes `自动识别章节` as the first/default item.
- GUI image search behavior:
  - If chapter dropdown is `自动识别章节`, GUI passes `chapter="auto"` into `MultiAgentCoordinator.search_loads(...)` with the Qwen classification result.
  - If Qwen chapter hint is accepted by coordinator, result metadata displays `章节：X（自动识别）`.
  - If route is `needs_chapter`, GUI shows warning `未能从题图自动识别章节，请手动选择章节后重新检索。` and does not search all chapters.
- Manual chapter priority:
  - If the user selects a concrete chapter (`2静定结构` ... `6力矩分配`), GUI passes that concrete chapter and does not use/override with auto chapter.
  - Result metadata displays `章节：X（手动选择）`.
- Manual load input with `自动识别章节` will also return `needs_chapter`; user should choose a concrete chapter for manual-load search.
- Verification:
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.
  - In-memory syntax compile for `gui.py` passed. Direct `python -m py_compile gui.py` was blocked by existing `__pycache__` permission on this machine, not by syntax.

## 2026-06-24 Feishu Multi Question MVP

- Goal: support photos/pages that contain multiple independent structure-mechanics questions without changing the existing single-question Feishu flow.
- Layout detection:
  - `scripts/classify_question_bank.py` now has `qwen_analyze_layout()` and `--layout-image`.
  - Output fields include `question_layout` (`single`/`multi`/`uncertain`) and per-question `label`, `bbox`, `loads`, `chapter_hint`, `chapter_confidence`, `chapter_evidence`.
  - `bbox` is retained for inspection/debugging, but the Feishu MVP does not use cropping.
- Feishu behavior:
  - Auto chapter mode first runs layout detection.
  - `single` or `uncertain` falls back to the old single-question flow unchanged.
  - `multi` starts a multi-question session and initially returns only a summary, e.g. question label, chapter status, and loads.
  - User replies with the question label to search that question using the recognized chapter.
  - User replies with `label-章节名` (for example `5-力法`) to override the chapter for that question.
  - After candidates are shown, user replies with `label-rank` (for example `6-2`) to get that answer.
  - In the multi-question candidate state, `0` returns to the multi-question list. In the list state, `0` ends the multi-question session.
- Retrieval rule:
  - Multi-question sessions now pre-crop structure diagrams at image receive time with OpenCV.
  - The CV path finds large line-art blocks by illumination normalization, adaptive thresholding, morphological close/dilate, and connected components.
  - Blocks are sorted top-to-bottom and bound to the layout question labels in order, e.g. labels `5/6/7/8` map to diagram blocks `1/2/3/4`.
  - When the user searches one question, the bot uses the pre-cropped diagram as `query_image_path` and forces Zhipu rerank even if the candidate pool is `<= rerank_top`.
  - If no crop is available, it falls back to load-only search.
  - Unknown chapters are not searched until the user specifies a chapter.
  - Questions identified as outside the current 2-6 chapter scope, such as influence-line questions, are reported as unsupported rather than searched.
- Verification:
  - Real page image with questions 5/6/7/8 was detected as `multi`.
  - The bot summary showed question 5 as unknown chapter, question 6 as `5位移法` with `均布:q`, question 7 as `6力矩分配` with `集中:200kN` and `均布:10kN/m`, and question 8 as unsupported influence-line scope.
  - OpenCV crop experiment on the same real page found four diagram blocks after filtering the top desk edge. Question 7 crop correctly contained the `200kN + 10kN/m` beam diagram.
  - Stable version timing on question 7: image receive/listing about `12.85s`; reply `7` returned in about `1.75s` with `已复筛`.
  - Replying `6` in a local bot dry-run searched question 6 in `5位移法` and returned Top 3 candidates.
  - Replying `0` from the candidate state returned to the multi-question list.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.

## 2026-06-24 Rerank Load Position Signal

- `search.RERANK_PROMPT` now scores candidates by both:
  - structural shape similarity;
  - load relative position/direction consistency.
- The old instruction `不要判断荷载位置` was removed.
- The prompt still tells the model not to solve the problem, not to recalculate exact load counts/types, and not to penalize question numbers, node letters, or dimension labels.
- Reason output was shortened to keep latency low.
- A/B check on the real multi-question page question 7:
  - previous shape-only prompt: about `4.85s` for 3 candidates in one run;
  - minimal shape+load-position prompt before shortening reasons: about `7.81s`;
  - final shortened official prompt: about `4.30s` for 3 candidates.
- Final checked scores on that sample:
  - exact matching candidate stayed `1.0`;
  - final wording `荷载位置和方向是否相同` scored the two mismatched candidates lower (`0.2` and `0.4` in the check), matching the goal of finding the same problem rather than loosely similar ones.

## 2026-06-24 Feishu No Match Diagnostics

- Feishu no-result replies now include diagnostic context instead of only saying `没有找到匹配题目。`
- Single-question no-match reply includes:
  - recognized/used chapter;
  - recognized loads;
- no route category or retry suggestion, to keep the Feishu message concise.
- Multi-question no-match reply includes the same context and keeps the question label.
- Search behavior is unchanged; this is only response text.
- Verification:
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.

## 2026-06-24 Unindexed Question Audit

- Added a read-only path scan tool for catching question images that exist on disk but are missing from Excel indexes.
- Command:
  - `python scripts/audit_unindexed_questions.py`
- Behavior:
  - Scans the configured live main root under chapter folders `2静定结构` through `6力矩分配`.
  - Only counts image files under directories whose segment starts with `题目`; skips answer/hidden/temp-style folders.
  - Loads both main chapter Excel files and the separate symbolic-bank Excel files, then treats the union of their `题目名称` paths as already stored.
  - Defaults to reading `special_unindexed_questions.json`; paths in that file are reported as special exclusions instead of missing records.
  - Uses case-insensitive normalized relative paths for matching to reduce Windows path casing false positives.
  - Writes a Markdown report under `.tmp_audit/` by default; optional `--json-out` can emit machine-readable output.
- Guardrail:
  - The audit does not call Qwen/Zhipu, does not classify loads, does not auto-store records, and does not modify live Excel.

## 2026-06-24 Store Unindexed Questions

- Added `scripts/store_unindexed_questions.py` to turn the path audit into an optional补库 workflow.
- Default command is dry-run:
  - `python scripts/store_unindexed_questions.py`
- Apply command:
  - `python scripts/store_unindexed_questions.py --apply`
- Behavior:
  - Reuses `audit_unindexed_questions.py` to find missing images, respecting `special_unindexed_questions.json`.
  - Calls the existing Qwen classifier only for missing images.
  - Uses `RuleRouter` to route recognized loads to main bank or symbolic bank; review/empty/unsupported cases are reported but not written.
  - Main-bank rows use the existing assigned-symbol normalization from `apply_main_bank_update.py`.
  - Symbolic-bank rows use the existing symbolic load mapping from `build_symbolic_bank.py`.
  - `--apply` appends rows only; it does not delete or edit existing rows. Before saving a touched workbook, it backs it up under `backups/store_unindexed_时间戳/`.
- Verification:
  - On 2026-06-24, dry-run found one real missing image: `6力矩分配/题目/5.jpg`.
  - It routed to main bank as `main_assigned_symbolic` with loads `集中:Fp=60kN` and `弯矩:40kN.m`.
  - `python scripts/store_unindexed_questions.py --apply` appended it to `6力矩分配.xlsx`.
  - Post-apply audit reported `scanned=705 special=7 missing=0`.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.

## 2026-06-24 GUI One-Click Audit Store

- GUI now has a top-right `一键审查` button in the input-mode frame.
- Behavior:
  - Runs in a background thread so the GUI does not freeze.
  - Uses the same audit/store functions as `scripts/store_unindexed_questions.py`.
  - Scans for unindexed question images, respects `special_unindexed_questions.json`, classifies only true missing images, routes to main/symbolic bank, appends ready records, and backs up touched Excel files.
  - After completion, a popup reports found/auto-storable/appended/needs-review/special counts and a per-chapter appended summary such as `5位移法：2题`; it intentionally does not show report/backup paths in the user-facing popup.
- Guardrail:
  - The GUI button does not reimplement classification/storage rules; it reuses the existing store-unindexed workflow.
  - `SKILL.md` was intentionally not updated for this internal GUI button.
- Verification:
  - `python -B -c "import gui; print('gui import ok')"` passed.
  - `python scripts/audit_unindexed_questions.py --limit 20` reported `scanned=705 special=7 missing=0`.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.

## 2026-06-27 Unitless Load Raw Rule

- The question-bank load `raw` field now stores no physical units. `type` carries the quantity family:
  - `集中:5`, `均布:5`, and `弯矩:5` are distinct because their `type` differs.
  - `5kN`, `5kN/m`, `5kN·m`, and `5kN.m` are normalized to `5`.
  - Assignments keep the symbol relation but drop units, e.g. `P=40kN` -> `P=40`, `q=20kN/m` -> `q=20`, `M=20kN·m` -> `M=20`.
- Extraction prompts in `search.py` and `scripts/classify_question_bank.py` now tell the model to classify load type by arrow/force-couple shape, not by the printed unit. This specifically handles typo images where a distributed load is mislabeled as `kN.m`.
- `search.strip_load_unit()` is the shared normalization entry for search/storage. `normalize_query_loads()` and `postprocess_extracted_loads()` both strip units.
- GUI single-image storage now uses the same Qwen classifier path as search/audit instead of the older Zhipu-only `extract_loads` path.
- Existing live Excel indexes were migrated with `scripts/normalize_excel_load_units.py --apply`.
  - Backup: `backups/normalize_load_units_20260627_112923`.
  - Rows scanned: 706; rows changed: 448; parse errors: 0.
  - Known correction applied: `5位移法/题目/3.jpg` is now exactly `{"loads":[{"type":"均布","raw":"5"}]}`.
- Verification:
  - Qwen classification on `5位移法/题目/3.jpg` returned only `均布:5`.
  - Unit residue scan over live Excel found `0` remaining `kN`/`kN.m`/`kN/m` raw values.
  - CLI/multi-agent search with both `均布:5` and `均布:5kN/m` found `5位移法/题目/3.jpg` at `100%`.
  - `python -B -c "import gui; print('gui import ok')"` passed.
  - `python scripts/feishu_tiku_bot.py dry-run-flow --image "D:\桌面\答疑、帮做\结构力学\帮做\5位移法\题目\3.jpg" --chapter 5 --choice 1` passed.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.

## 2026-06-27 Static Structure Diagram Chapter Guard

- Auto chapter detection now accepts `2静定结构` when the evidence contains `静定` plus an internal-force diagram phrase:
  - `内力图`
  - `弯矩图`
  - `剪力图`
  - `轴力图`
- The rule intentionally does not care whether the text says beam, frame, multi-span beam, or another structure. Examples such as `静定梁弯矩图和剪力图`, `静定多跨梁弯矩图`, and `静定钢架内力图` should all be accepted as `2静定结构`.
- Guardrail retained: evidence with only `内力图/弯矩图/剪力图` and no `静定` remains `unknown`; evidence containing `超静定` or `不静定` is not accepted as `2静定结构`.
- Verification: `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.

## 2026-06-29 Handoff Notes

- New chat minimal context:
  1. Read `AGENTS.md`.
  2. Read the last sections of `.agents/project_memory.md`, especially `Unitless Load Raw Rule`, `Static Structure Diagram Chapter Guard`, and this handoff section.
  3. Read `SKILL.md` sections `当前检索流程` and `注意事项`.
  4. Only then read task-specific code:
     - Feishu: `scripts/feishu_tiku_bot.py`.
     - GUI: `gui.py`.
     - Auto chapter / retrieval chain: `multi_agent_pipeline.py` and `scripts/classify_question_bank.py`.
     - Store/audit: `scripts/store_unindexed_questions.py`, `scripts/audit_unindexed_questions.py`.
- Current important behavior:
  - Load `raw` is unitless everywhere. Do not reintroduce `kN/kN/m/kN·m` into Excel raw values; type carries the unit family.
  - GUI image search should display only Top 3 results even when rerank is skipped; do not force rerank merely to reduce display count. The displayed Top 3 must also be written back to `_last_search.json` so `打开答案` matches the visible ranking, same as Feishu sessions.
  - Feishu now adds the configured OK reaction for all accepted message types, not only image messages. Existing running bot must be restarted before this code is live.
  - Feishu no-match replies include recognized chapter and loads.
  - Feishu skipped-rerank replies include the reason, e.g. rerank candidates not exceeding `rerank_top`.
- Current Feishu multi-question behavior:
  - Qwen layout analysis still decides `single/multi/uncertain` and extracts each question's label/load/chapter.
  - Multi-question diagram preparation no longer calls Qwen once per question/candidate. It first lets OpenCV find candidate line-art blocks, locally filters obvious non-structure blocks, and directly binds top-to-bottom when the filtered block count equals the question count.
  - If local filtering does not produce exactly the question count, the bot builds a numbered contact sheet of OpenCV blocks and calls Qwen once to select complete structure diagrams. If that still does not match the question count, it returns the multi-question list without pre-cropped diagrams; the later search falls back to load-only behavior.
  - Timing logs are written to `tiku_bot.err.log`, including layout seconds, crop seconds, local block counts, Qwen block-filter seconds, and total seconds.
- Recent data deletion:
  - Deleted from live bank per user request: `2静定结构/3钢架/2弯矩图/题目a/2门/11.jpg`.
  - Deleted corresponding answer file: `2静定结构/3钢架/2弯矩图/答案/11.jpg`.
  - Removed its Excel row from `2静定结构.xlsx`; backup is `backups/delete_question_20260629_104110/2静定结构.xlsx`.
  - Verification after deletion: `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.
- Latest verification before handoff:
  - `python scripts/smoke_test.py` passed after GUI Top3 display fix and after the deletion.
  - `git status --short --branch` was clean after commit `7dd566c Show top 3 GUI image results`, before this memory-only update.

## 2026-06-30 Feishu Multi Question UX and Crop Speed

- Feishu multi-question list output was shortened for mobile readability:
  - Each question now uses separate lines for chapter and loads, e.g. `第四题：章节：静定结构` then `荷载：均布：1、集中：2`.
  - Chapter numbers are hidden in the summary display only; internal chapter names remain unchanged for search.
- After a multi-question answer command such as `4-1`, the bot no longer repeats the full candidate summary. It sends the answer image plus a short `请继续回复：` action list.
- Multi-question crop flow was changed to reduce slow Qwen verification:
  1. Qwen whole-image layout identifies question count, labels, loads, and chapter hints.
  2. OpenCV finds candidate diagram/line-art blocks.
  3. Local rules filter obvious non-structure blocks such as title text strips, page/footer strips, tiny dimension fragments, and watermark-like blocks.
  4. If the filtered count equals the question count, blocks are bound to questions top-to-bottom without calling Qwen verify.
  5. If counts differ, the bot builds a numbered contact sheet and asks Qwen once which blocks are complete structure diagrams.
  6. If Qwen-selected count equals the question count, blocks are bound top-to-bottom.
  7. If counts still differ, pre-cropping is abandoned and the multi-question list is returned; selecting a question later falls back to load-only search.
- Old per-question/candidate `qwen_verify_diagram()` verification was removed from the Feishu multi-question receive path.
- Measured on a real 3-question page:
  - OpenCV found 6 blocks; local filter kept the 3 real structure diagrams in about `0.07s`.
  - The one-shot Qwen block-filter fallback selected blocks 2/3/4 in about `3.2s`.
  - Expected receive latency is now mainly Qwen layout time plus local crop time, with one-shot Qwen fallback only when local filtering is ambiguous.
- Verification:
  - `python -B -c "import scripts.feishu_tiku_bot; print('feishu_tiku_bot import ok')"` passed.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.
  - Existing running Feishu bot must be restarted before the new crop flow and message formatting are live.

## 2026-06-30 Feishu Store Mode MVP

- Feishu tiku bot now has a conservative single-question store mode in the same bot/service, not a separate Feishu app or port.
- Commands:
  - `+` enters store mode.
  - `0` cancels the current store flow.
  - During answer collection, `1` means answer images are complete and moves to the confirmation page.
  - On the confirmation page, `1` confirms and writes to the question bank.
- First-version scope:
  - One question image.
  - One or more answer images.
  - Store only under the chapter root folders, e.g. `4力法/题目/` and `4力法/答案/`; no subfolder classification yet.
  - Answer naming follows the existing plus convention: `31.jpg`, `31+.jpg`, `31++.jpg`.
  - New question number is computed as max existing numeric prefix in the chapter root `题目`/`答案` folders plus one, with overwrite checks before writing.
- Safety behavior:
  - Nothing is copied to the live bank and no Excel is written before the user confirms.
  - If auto chapter is missing or low confidence, the bot asks the user to choose `2/3/4/5/6`, matching search-mode chapter handling.
  - Empty loads or `needs_review` routing are not auto-stored.
  - On confirm, the bot writes the question image, answer image(s), and appends the Excel row after backing up the target workbook.
  - Main vs symbolic Excel routing uses the existing `RuleRouter`: numeric/assigned-symbol loads go to the main bank; unassigned symbolic loads go to the symbolic Excel bank while images still live under the main bank chapter folders.
- Implementation:
  - `scripts/feishu_store_flow.py` contains store-mode planning, numbering, naming, JPEG saving, backup, and Excel append logic.
  - `scripts/feishu_tiku_bot.py` only owns the Feishu entry point and state transitions.
- Verification:
  - Dry-run store flow was tested with mock Qwen through `+ -> question image -> multiple answer images -> 1 -> 1`.
  - Live-bank dry-run planning was checked on examples from `4力法`, `5位移法`, and `6力矩分配`; no live files or Excel were written.
  - `python scripts/smoke_test.py` now includes a dry-run store-mode state check and passed with `SUMMARY PASS warnings=0`.
  - Existing running Feishu bot must be restarted before store mode is live.

## 2026-07-02 Feishu Candidate Delete Flow

- Feishu search candidate pages now support deleting wrong bank entries.
- Commands:
  - Single-question candidate page: `-1`, `-2`, or `-3` starts deletion for that ranked candidate.
  - Multi-question flow: first enter a specific question's candidate page, then use `-1`, `-2`, or `-3` to delete the current question's ranked candidate.
  - Delete confirmation uses `1` to execute and `0` to cancel.
- Safety behavior:
  - Delete only works when a candidate list is active; store mode and normal idle text do not treat `-1` as delete.
  - `scripts/feishu_delete_flow.py` prepares a delete plan from the selected candidate path and current chapter.
  - It searches both the main bank and `帮做_字母库` workbook for exact matching `题目名称` rows, so symbolic-bank candidates delete the symbolic Excel row while image files still live under the main chapter folder.
  - Execution backs up the touched workbook(s), moves the question image and all matching answer images into `backups/delete_question_时间戳/files/`, then deletes the matched Excel rows.
  - If file movement fails, Excel rows are not deleted.
- UX behavior:
  - Confirmation text shows chapter, question path, answer filenames, touched bank(s), and row count.
  - After a successful delete, the selected candidate is removed from the in-memory session result list; remaining candidates can still be answered or deleted.
- Verification:
  - `python -B scripts\smoke_test.py` passed with `SUMMARY PASS warnings=0`.
  - Isolated escalated verification on a temporary mock bank confirmed Excel row count became `0`, original question/answer files were removed from their original locations, and two files were moved into `backups/delete_question_20260702_100152/`.
  - Existing running Feishu bot must be restarted before candidate deletion is live.

## 2026-07-02 Symbolic Bank Structure Type Tagging

- To reduce expensive visual rerank pressure from too many `100%` symbolic-load candidates, the first extra筛选因素 was scoped to coarse structure type, not support count or member count.
- Final first-version categories:
  - `梁`
  - `钢架`：includes 刚架、框架、门架、闭口框架、组合结构.
  - `桁架`
  - `拱`
  - `unknown` only for unclear/incomplete images.
- Added read-only evaluation script:
  - `python scripts/evaluate_qwen_structure_type.py --limit 20 --random --seed 20260703 --retries 1`
  - Markdown reports include inline images for manual review.
- Random 20-image evaluation after switching from `其他` to `桁架`:
  - Report: `.tmp_support_eval/qwen_structure_type_20260702_153827/summary.md`
  - Request success: `20/20`
  - Known path-label matches: `15/15`
  - Average latency: about `2.14s`, max `2.50s`.
- Added batch write script:
  - `scripts/write_symbolic_structure_types.py`
  - Default mode is classification/dry-run; `--apply` writes Excel after backup.
  - `--from-results` can reuse a saved `classification_results.json` without re-calling Qwen.
- Live symbolic bank Excel files under `D:\桌面\答疑、帮做\结构力学\帮做_字母库` were updated with a single new column: `结构类型`.
- Backup before writing:
  - `backups/symbolic_structure_types_20260702_155415`
- Final live symbolic-bank distribution after one manual correction:
  - `2静定结构.xlsx`: `钢架 42`, `桁架 37`, `梁 10`, `拱 4`
  - `3静定结构位移.xlsx`: `钢架 13`, `梁 12`, `桁架 3`
  - `4力法.xlsx`: `钢架 60`, `梁 20`, `桁架 3`
  - `5位移法.xlsx`: `钢架 49`, `梁 3`
  - `6力矩分配.xlsx`: `钢架 4`, `梁 3`
- Manual correction:
  - `2静定结构/5桁架/指定杆/题目2/46.jpg` was changed from `钢架` to `桁架`; despite containing beam/diagonal/support-like elements, it belongs to the truss set by project semantics.
- Verification:
  - Live symbolic Excel columns are exactly `题目名称`, `荷载`, `结构类型`.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.
- Next implementation step: use `结构类型` only to reorder/tie-break symbolic-bank `100%` candidates before Zhipu rerank; do not hard-filter candidates by structure type.
- 2026-07-02 timing experiment added:
  - Script: `scripts/experiment_structure_type_filter.py`
  - Query sample: `4力法/1梁/1单未知量/题目/10.jpg`, symbolic load `均布:q`, query structure type `梁`.
  - Old flow `章节 -> 荷载排序 -> Zhipu复筛`: ranked/reranked `29` candidates, total about `48.98s`.
  - New experimental flow `章节 -> 结构类型筛选 -> 荷载排序 -> Zhipu复筛`: filtered to `9` candidates; structure recognition about `2.49s`, rerank about `12.44s`, total with structure recognition about `14.95s`.
  - On this sample, structure type filtering reduced rerank candidates by about `69%` and saved about `34s`.
  - Experiment report: `.tmp_support_eval/structure_filter_compare_20260702_160939/summary.md`.
- 2026-07-02 official pipeline integration:
  - Added shared helper `scripts/structure_type_classifier.py`.
  - `MultiAgentCoordinator.search_loads()` now applies structure type filtering only when:
    - route is `symbolic`;
    - `query_image_path` exists;
    - Qwen returns a non-unknown structure type;
    - the symbolic-bank Excel has a `结构类型` column with at least one matching row.
  - Main bank searches and manual load-only searches are unchanged.
  - Feishu single-question image searches and multi-question diagram-crop searches use the same coordinator path, so symbolic image searches now follow `章节 -> 结构类型筛选 -> 荷载排序 -> Zhipu复筛`.
  - If structure recognition fails or no matching `结构类型` rows exist, retrieval falls back to the previous load-only ranking.
  - Smoke test now includes a temp Excel check that `structure_type="梁"` keeps only matching symbolic candidates while unfiltered ranking still keeps both perfect candidates.
  - Verification:
    - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.
  - Direct coordinator check on `4力法/1梁/1单未知量/题目/10.jpg` returned `route=symbolic`, `structure_type=梁`, `structure_filter_applied=True`, `reranked=True`, Top 1 exact same image.
  - The direct coordinator check showed a sandbox-only warning when writing live `_last_search.json`; this is due to the current Codex filesystem sandbox and does not affect the official service logic.
- Main-bank check after symbolic integration:
  - The largest exact load-signature group in the live main bank is `4力法` with `均布:10`, only `10` candidates; all are effectively steel-frame type by path/visual semantics.
  - Running the official coordinator on `4力法/2钢架/1单未知量/题目1/1L/1提横/2固+饺/34.jpg` took about `16.56s`, with `route=main`, `reranked=True`, and Top 1 exact same image.
  - Top repeated main-bank groups mostly have only `4-10` candidates, and hypothetical structure filtering would usually reduce only `0-2` candidates while adding a Qwen structure call. Current decision: do not add structure-type filtering to the main/numeric bank yet.

## 2026-07-02 Symbolic Residue Moved Out Of Main Bank

- User noticed a `均布:q` item still in the main/numeric bank; a scan found two unassigned symbolic-load residues in live main Excel files.
- Moved both rows from `D:\桌面\答疑、帮做\结构力学\帮做` to `D:\桌面\答疑、帮做\结构力学\帮做_字母库`, keeping the question images in their original chapter folders:
  - `5位移法/2钢架/题目/4.jpg`, load `均布:q`, added to symbolic `5位移法.xlsx` with `结构类型=钢架`.
  - `2静定结构/3钢架/2弯矩图/题目a/12.jpg`, load `集中:P`, added to symbolic `2静定结构.xlsx` with `结构类型=钢架`.
- Backup before writing:
  - `backups/move_symbolic_residue_20260702_163806`
- Verification:
  - Live main-bank scan for unassigned symbolic raw values returned `count 0`.
  - Both moved rows are absent from main Excel and present once in symbolic Excel.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.
- Later visual recheck of main-bank images found additional confirmed unassigned symbolic-load residues. These were moved from main Excel to symbolic Excel after backup:
  - `2静定结构/4拱/1内力/题目/5.jpg`: old main row had `均布:2` from adjacent-image contamination; moved as `均布:q`, `结构类型=拱`. Backup: `backups/move_arch_symbolic_20260702_172954`.
  - `3静定结构位移/3钢架/题目aa/26.jpg`: moved as `集中:P`, `结构类型=钢架`.
  - `4力法/2钢架/1单未知量/题目1/0/12.jpg`: removed from main and updated existing symbolic row to `集中:F`, `结构类型=钢架`.
  - `4力法/2钢架/1单未知量/题目1/3门/20.jpg`: moved as `均布:q`, `结构类型=钢架`.
- Backup for the three-row review batch:
  - `backups/move_review_batch_symbolic_20260702_174140`
- Verification after the review-batch move:
  - The three rows are absent from main Excel and present once in symbolic Excel.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.

## 2026-07-02 Chapter 7 Matrix Displacement Added

- Added `7矩阵位移` as the next supported chapter before adding influence-line support.
- Code/docs updated so the chapter is available to:
  - Qwen chapter hint normalization and prompts in `scripts/classify_question_bank.py`.
  - unindexed-image audit/store scripts.
  - Feishu chapter parsing and manual chapter prompts; replying `7` maps to `7矩阵位移`.
  - Feishu store flow chapter validation.
  - smoke-test expected chapter checks.
  - README / project Skill chapter-scope wording.
- Live main bank:
  - Created `D:\桌面\答疑、帮做\结构力学\帮做\7矩阵位移.xlsx`.
  - Used existing `scripts/store_unindexed_questions.py` storage flow for `7矩阵位移`.
  - Scanned `13` question images under `7矩阵位移`; all were ready for main bank, none required symbolic-bank routing or manual review.
  - Appended `13` rows to `7矩阵位移.xlsx`.
- Verification:
  - `python scripts/audit_unindexed_questions.py --chapter 7矩阵位移 --no-special-index` reported `scanned=13 special=0 missing=0`.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`, including `7矩阵位移: Excel readable rows=13`.
- Later the user added folder `7矩阵位移/2杆端力1+2`; it was processed through the same storage flow:
  - New scan found `8` missing images.
  - Appended `6` rows to main bank `D:\桌面\答疑、帮做\结构力学\帮做\7矩阵位移.xlsx`.
  - Appended `2` unassigned-symbolic rows to symbolic bank `D:\桌面\答疑、帮做\结构力学\帮做_字母库\7矩阵位移.xlsx`.
  - Backup directory: `backups/store_unindexed_20260702_181454`.
  - Verification reported `scanned=21 special=0 missing=0`; main workbook has `19` rows and symbolic workbook has `2` rows.
## 2026-07-02 Chapter 8 Influence Line Added

- Added `8影响线` as a supported chapter after `7矩阵位移`.
- Code/docs updated so the chapter is available to:
  - Qwen chapter hint normalization and prompts in `scripts/classify_question_bank.py`.
  - unindexed-image audit/store scripts.
  - Feishu chapter parsing and manual chapter prompts; replying `8` maps to `8影响线`.
  - Feishu store flow chapter validation.
  - smoke-test expected chapter checks.
  - README / project Skill chapter-scope wording.
- Live main bank:
  - Created `D:\桌面\答疑、帮做\结构力学\帮做\8影响线.xlsx`.
  - Used existing `scripts/store_unindexed_questions.py` storage flow for `8影响线`.
  - Scanned `21` question images under `8影响线`; all were ready for main bank, none required symbolic-bank routing or manual review.
  - Appended `21` rows to `8影响线.xlsx`.
  - Apply backup directory was `backups/store_unindexed_20260702_182047`; since `8影响线.xlsx` was newly created, there was no previous `8影响线.xlsx` workbook to copy into the backup.
- Verification:
  - `python scripts/audit_unindexed_questions.py --chapter 8影响线 --no-special-index` reported `scanned=21 special=0 missing=0`.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`, including `8影响线: Excel readable rows=21`.
- Entry-point verification after adding chapters 7/8:
  - GUI chapter dropdown is driven by live root `*.xlsx`; it now lists `7矩阵位移` and `8影响线`.
  - GUI one-click audit/store uses `scripts.audit_unindexed_questions.CHAPTERS`, now including 7/8.
  - Feishu manual chapter prompt lists 2-8; `parse_chapter("7") -> 7矩阵位移`, `parse_chapter("8") -> 8影响线`.
  - Feishu store dry-run planning accepts `8影响线` and targets `8影响线.xlsx`.
  - Feishu delete dry-run planning finds rows in `7矩阵位移.xlsx` and `8影响线.xlsx`.
  - CLI/multi-agent retrieval was tested on both `7矩阵位移` and `8影响线`; both returned candidates from the expected chapter.
  - `scripts/feishu_tiku_bot.py` dry-run MockCoordinator was corrected so manual dry-run chapter 7/8 echoes the selected chapter, while `chapter=auto` still simulates an auto-detected `5位移法`.
  - `python scripts/smoke_test.py` passed with `SUMMARY PASS warnings=0`.
