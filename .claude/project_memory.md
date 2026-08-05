# Project Memory

## Current State

- 项目位于 `F:\cc\7-题库检索`，用于结构力学题库检索与维护。
- 8788 是现有飞书题库机器人（固定公网回调）；8790 是稳定网页主线，生产状态 `.tmp_tiku_agent_v2_prod_8790`，业务走 Intent V2 固定编排。
- 8793 是长期复用的观测与人工评审层，镜像 8790，独立状态 `.tmp_review_tiku_prod_8793`、运行身份 `review-8793-prod`。
- 8794 位于 `F:\cc\7-题库检索-8794` 独立 worktree，源码/端口/Cookie/状态/输出均与 8788/8790/8793 隔离；自主化暂缓，不同时引入 LangGraph。
- 当前工作线：8790 结构尺寸低成本预筛（字母库第三道硬过滤）。字母库「长×宽」列全量回填已完成：280 行中 276 行已填（19 条人工裁决 + 257 条千问 qwen3.7-plus v4 识别），4 行留空（均为拱，高度需计算按设计 null）。
- 已定决策：① 回填模型=千问 qwen3.7-plus（与生产同口径）；② DIMENSION_PROMPT 用分段转录 v4（各段与 total_span 独立、禁止凑一致、代码求和权威、rotation-invariant），全量回填即此版本，取代早年"钢架/桁架直接长×宽简化"；③ 多段加法错误兜底暂缓。
- 8790 模型费用台账只写入 `.tmp_tiku_agent_v2_prod_8790/model_costs.sqlite3`；8788 管理员查询以 `mode=ro`+`query_only` 只读，绑定身份与游标仅存 8788 的 `.tmp_feishu_tiku`。

## Implemented

- CLI、飞书和 Agent 共用章节2–8、题图/荷载识别、数值/字母库路由、粗筛、shape-only 视觉复筛和答案定位；章节扫描统一到 `search.scan_chapter_candidates`。
- Intent V2 是唯一 Agent 运行时：规则处理明确表达，Qwen处理上下文长尾，代码校验状态、权限、参数和编号命名空间。
- 支持单题/多题、章节纠正、候选切换/否定、答案回看、非重复续搜、失败恢复及经明确授权的一次严格全局搜索。
- SQLite 会话和媒体可跨刷新、重启恢复；网页 Demo 支持移动端、裁剪上传、拖放、进度、候选按钮和大图查看。
- 8793旁路记录意图、授权、五态工具结果、状态与最终结果，评审标签不修改业务状态；日常摘要面向结果，技术字段留在折叠详情，且不记录图片、用户原文、绝对路径、模型原文或敏感信息。
- 候选按钮以题目版本和候选批次共同校验，拒绝新题后的旧按钮；同题同批次在答案后仍可改选。8790与8793数据库独立，8793启动校验固定运行身份。
- 8794 独立入口/端口/Cookie/运行目录与输出；多题归一、章节判定、裁图与 Qwen 图块筛选在 `tiku_shared.multi_question`，8788 保留兼容入口。
- 8790 与 8794 均启用状态感知安全回答 V0：安全闲聊只收六字段脱敏状态摘要，模型输出经状态一致性/编造/执行声明/泄密校验，不合格则固定兜底（真实千问矩阵 70 条中 62 直出、8 兜底）。
- 九个业务工具及候选动作统一返回结构化五态 `SUCCESS / NO_MATCH / NEEDS_INPUT / PARTIAL / TOOL_ERROR`；8793展示中文结果、原因码、可重试性和状态变化。
- 智谱复筛默认 `glm-4.6v`、10 路并发并校正 EXIF；普通章节只展示 `>=80%` 可靠结果（`>=90%` 展示全部高分，80–90% 仅最高 1 道），失败或不完整回退粗筛。
- 唯一候选时支持短肯定直接确认；多候选、已显示答案和复合表达不会自动选择。历史 Tk 端保留为 `legacy_gui.py`，入库执行 plan→confirm→backup→execute。
- 飞书事件去重为30分钟TTL、20,000条上限和加锁缓存；8788运行当前主线代码。
- 模型费用台账按一次搜题聚合千问/智谱每次调用、尝试次数、token、版本化价格、估算费用与告警，SQLite 写失败不影响搜题，CLI 可按天/模型/单次搜题查询；费用记录不增加模型调用，全量 386 项测试通过。
- 8788 管理员费用查询：发送者显式一次性本地绑定 + 配置白名单共同授权；按同一题的 `search_key` 汇总该题所有模型调用，严格超过 0.05 元单列显示，金额保留四位小数；已端到端验收。
- 结构尺寸识别设施：`scripts/evaluate_structure_dimensions.py`（千问+外部视觉对比、review.html）、`run_zhipu_dimension_recognition.py`（智谱 glm-4.6v 直连回退）、`backfill_letter_bank_dimensions.py`（备份后写字母库「长×宽」列）、`backfill_run_dimensions_qwen.py`（千问 v4 并发全量识别→results.json+verdicts，`--reuse-results` 免重跑重生成）；manifest 与人工裁决在 `experiments/structure_dimension_eval/`。

## In Progress

- 结构尺寸预筛下一阶段：`dimensions=` 硬过滤接入 `scan_chapter_candidates`/`coarse_search_tool`（query 侧千问 v4 尺寸识别 + `dimensions_match`，拱 skip、query 未验证跳过滤保召回，实验开关对照）——尚未开始，见 Next Best Step 1。

## Not Implemented

- 尚未实现AI影子规划、计划权限审核、有限自主执行器、一次重规划和调用预算。
- 8793评审记录尚不会自动进入Codex排查上下文；用户说“已经标记”时需主动读取固定评审目录并关联turn/trace。
- 尚未接入LangGraph/checkpoint、新飞书Agent、真实流量统计、访问配额和身份认证。
- 结构尺寸硬过滤尚未接入 8790 候选流程（`scan_chapter_candidates`/`coarse_search_tool`）；新旧流程对比与真实入口验收未完成（全量维度回填已完成）。

## Architecture Rules

- 8790保持稳定；8794必须使用独立 worktree、实验分支、端口、Cookie、输出和运行目录，不能复用8790/8793/8788源码工作区或状态。
- 8793是可复用测试外壳，持续镜像当前8790的确定提交；后续8794阶段先独立验证与评审，再按小阶段提升到8790并同步8793镜像。
- 8794不包含8793完整技术评审侧栏；测试、轨迹和逐事件标注继续留在8793。
- 固定编排处理快速路径；AI只在长尾内提出计划，所有步骤由代码权限层审核。
- 工具是否成功、未命中、部分完成或报错由结构化协议和代码确定，AI只选择合法路径并解释结果。
- 每回合最多规划一次、意外结果最多重规划一次、工具数有硬上限、同一失败工具最多重试一次。
- AI不得擅自跨章节、降低阈值、跳过状态、使用旧候选、编造答案或执行写操作。
- 先做影子规划；LangGraph待8794行为稳定后作为独立阶段评估，不与有限自主同时引入。

## Known Risks

- 浏览器历史与服务端状态曾形成两个真相；旧候选已修复，8794仍需以服务端状态和 `allowed_actions` 为权威。
- 评审记录曾因错误运行目录分散；生产8793已固定目录，但自动问题交接闭环尚未实现。
- 有限自主可能增加延迟和成本；必须保留快速路径、单次规划和明确预算，并比较完整任务耗时。
- 题库约800道，合理未命中常见；不能把未命中率直接当Agent错误，也不能为命中降低门槛。
- 真实图片变体、非同文件高分题和外部模型稳定性样本仍不足；开发集不能外推真实泛化能力。
- 2026-07-29一次8794启动命令曾把两枚模型API密钥展开到本机进程命令行和工具输出；相关进程已立即终止并改为安全环境继承。用户明确选择暂不轮换旧密钥，功能可继续，但残余泄露风险仍由用户承担。
- 当前会话模型无法看图（Read 图片返回 Unsupported Image）且视觉 MCP 被禁用/拒绝；Claude 侧视觉识别需支持视觉的会话或人工对照题图完成。
- 多段尺寸求和曾是共同短板；v4 反编造子句已修复（beam_continuous 6L→5L），回填与未来查询识别同用千问 v4 口径，口径一致风险降低；兜底策略仍暂缓。frame_t 双模型判「组合结构」而目录为钢架，不影响尺寸提取。

## Do Not Do

- 不提交或展示密钥、完整本地配置、题库资产、运行日志和人工评审数据。
- 不回退用户已有修改，不把全局记忆写入项目文件。
- 不在8794开发中改造8788，也不复用8790/8793运行状态。
- 不直接修改live题库；写操作必须确认并备份。
- 不在8794同时引入有限自主和LangGraph。
- 不把自由多工具循环、默认跨章搜索或自动降低阈值当作自主能力。
- 不直接复用生产荷载/章节分类 Prompt 做结构类型与尺寸识别实验；实验验证前不修改 8790 生产、不硬截断 22 个候选。

## Next Best Step

1. 将 `dimensions=` 硬过滤接入 `scan_chapter_candidates`/`coarse_search_tool`：章节路由后对 query 题图做一次千问 v4 尺寸识别（复用 `DIMENSION_PROMPT`/`normalize_provider_result`，一次模型调用），在结构类型匹配后插入 `dimensions_match`（拱返回 skip；query 侧 `dimensions_verified` 为 False 时跳过硬过滤保召回）；加实验开关默认关，跑开/关对照看 22 候选压到多少、有无错删正确候选。
2. 真实入口验收：从 `Agent.handle_text` 全链路进入、开关对照、金标准样本、细分类统计、空计划不算成功、多轮稳定性；通过后再考虑 8790 生产接入。实验验证通过前不修改 8790 生产、不硬截断 22 候选。
3. 若有疑似错值，用 `scripts/backfill_run_dimensions_qwen.py --reuse-results <results.json>` 免重跑重生成 verdicts，复核后经 `scripts/backfill_letter_bank_dimensions.py` 回写（先备份）。

## Important Commands

```powershell
Set-Location -LiteralPath "F:\cc\7-题库检索-8794"
python -B -m unittest discover -s tests -p "test_*.py"
Set-Location -LiteralPath experiments\decision_trace_lab
python -B -m unittest discover -s tests\mainline_parity -p "test_*.py"
python -B "F:\cc\7-题库检索-8794\scripts\run_tiku_agent_8794.py"
Invoke-RestMethod http://127.0.0.1:8790/health
Invoke-RestMethod http://127.0.0.1:8794/health
Invoke-RestMethod http://127.0.0.1:8793/api/observation/source
```
