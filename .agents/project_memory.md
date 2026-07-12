# Project Memory

## Current State

- 项目位于 `F:\cc\7-题库检索`，用于维护结构力学题库并按图片或荷载检索相似题、返回答案。
- CLI、现有飞书检索/入库/删除、章节 2-8、主库/字母库分流和字母库结构类型筛选处于可用维护阶段。
- 独立 `tiku_agent/` 已完成工具层、LLM intent、对话状态，以及单题/多题编排 MVP；LangGraph checkpoint 和新飞书机器人尚未接入。
- Agent 使用 `.tmp_tiku_agent`，现有飞书使用 `.tmp_feishu_tiku`；两套运行状态必须持续隔离。
- 视觉复筛采用 GLM shape-only 轮廓评分，与粗筛荷载分各占 50%；CLI、飞书 pipeline 和 Agent 工具共享并发、超时补评和整批回退策略。
- 验证入口是 `python -B scripts/smoke_test.py`；完整单元测试使用 `python -B -m unittest discover -s tests -p "test_*.py"`。
- 本地配置可能包含路径或密钥，只查看 `config.example.json` 的结构，不读取或提交完整本地配置。
- 当前分支为 `codex/llm-rerank-support-filter`；`data/feishu_chapter_failure_log.jsonl` 是运行日志，不混入普通提交。

## Implemented

- `search.py`：粗筛、并发视觉复筛、答案定位和基础 CLI；默认 `top_k=3`，本地配置可覆盖。
- `multi_agent_pipeline.py`：图片识别、章节判断、主库/字母库路由、结构类型筛选和复筛协调。
- `scripts/search_by_loads.py`：`loads-search`、`image-search`、`answer` 的轻量 CLI 包装。
- `scripts/feishu_tiku_bot.py`：现有飞书单题/多题检索、章节选择、答案返回、入库和候选删除。
- `scripts/feishu_store_flow.py` 与 `scripts/feishu_delete_flow.py`：带计划、确认和备份的题库写操作。
- `tiku_agent/tools.py`：隔离的分析、路由、结构分类、粗筛、复筛、候选选择和答案工具。
- `tiku_agent/intent.py`：Qwen LLM-first intent，支持搜题、改章节、选择题目/候选、重发答案和取消。
- 候选页、已答题页和待选章节页中，用户明确说出 2-8 章时，规则解析优先于 LLM，直接作为章节纠正处理。
- `tiku_agent/state.py`：支持章节纠正、多题当前裁图、候选切换、答案回看和错误恢复。
- `tiku_agent/agent.py` 与 `render.py`：单题/多题自然语言编排；多题可选题号、用对应裁图检索并复用候选和答案流程。
- `tiku_agent/render.py`：新 Agent 的所有用户可见检索话术统一为简洁自然对话，隐藏路径、分数和内部状态；唯一候选支持“就这个 / 要这个 / 发答案”直接取答案。
- `tiku_agent/session_store.py`、`session_runtime.py` 与 `session_artifacts.py`：隔离 SQLite 会话存储、外层恢复/保存和 session 临时文件管理已实现；默认最后活跃后 2 小时过期，取消即清除状态与题图/裁图/答案临时文件。真实单题已验证“候选→模拟重启→就这个→答案”。
- `tiku_agent/task_log.py`：已定义隐私受限的结构化任务日志契约；仅允许耗时、阶段、结果、候选数、章节、路由和错误类别，写入后端尚未实现。
- `tiku_agent/tools.py`：首轮 scope 只判单题/多题；单题同时返回荷载/章节并直接检索，多题才调用详细题号/bbox/逐题荷载识别和裁图准备。
- 两套荷载提取 prompt 已统一：赋值符号输出无单位的 `符号=数值`；`P=40/q=20/M=20` 路由主库，纯符号仍路由字母库，并有回归测试。
- 共享复筛不再因候选少于 `rerank_top` 而跳过；候选达到路由粗筛阈值就进入复筛。
- 并发复筛最多 10 个候选；首轮单候选 8 秒，超时项最多补评 3 个、每项 10 秒，仍不完整则整批回退粗筛排序。
- 视觉复筛只看主杆件骨架、整体轮廓、主要杆件数量和连接位置，忽略荷载、尺寸、文字、题号和支座细节。
- 用户可见分数为 `0.5 * 粗筛荷载分 + 0.5 * 视觉轮廓分`，复筛输出遵守 90/80 阈值规则。

## In Progress

- Agent 处于多题 MVP 后的迭代阶段；首轮条件分流已完成，待补图片附带文字的显式单题分流和真实多题图端到端回归。

## Not Implemented

- 尚未引入 LangGraph 图、独立 FastAPI 入口或结构化任务日志写入；会话恢复和 session 临时文件清理已接入 Agent 外层并完成真实验证。
- 尚未创建或连接新的飞书机器人；现有飞书机器人不是 Agent 入口。
- 复筛并发数、首轮超时和补评上限尚未配置化。
- 缺少 pipeline 级超时回退、真实飞书事件和持久状态恢复集成测试。

## Architecture Rules

- 新 Agent 必须使用独立入口、配置、机器人凭据、事件 URL、端口、隧道、session、日志、checkpoint、临时图片和测试输出。
- 不停止、重启或改造正在使用的 `scripts/feishu_tiku_bot.py` 服务来测试 Agent；是否重启由用户另行决定。
- 早期 Agent 可以只读使用 live 题库；入库、删除和修复必须走 `plan -> confirm -> execute` 并备份。
- 章节必须由用户指定、确认，或由 `chapter=auto` 根据题干/题型文字证据确定；纯结构图不能跨章节猜测。
- 荷载类型只使用 `集中`、`均布`、`弯矩`；`raw` 保留原始标注并做单位归一化。
- 主库保存数值荷载和已赋值符号题，字母库保存未赋值字母题；字母库图片检索才使用结构类型筛选。
- 复筛只处理粗筛候选，不反向改变候选池；未完成批次不能混合视觉分形成最终排序。
- 默认视觉复筛继续使用 GLM shape-only；10 组对比中 GLM 为 9/10、Qwen 为 7/10，Qwen 对关键骨架不同的异形题压分不足。除非有新评测证据，不切换默认模型。
- `_last_search.json` 按最终显示结果重写，保证答案序号和用户所见排名一致。
- 修改检索、储存、索引或 Agent 共享逻辑时，同步检查 CLI、飞书、Agent 和项目 Skill 文档。

## Known Risks

- 同粗筛分的超时候选补评顺序可能受线程完成顺序影响，尚未建立确定性次序。
- `search.py` 和 `scripts/feishu_tiku_bot.py` 体量较大；章节和中文序号解析仍有多处实现，存在行为漂移风险。
- 单元测试以 mock 和纯函数为主，外部模型、飞书事件和状态恢复覆盖不足。
- 自动图片入口首轮会判定单题/多题；真实第七章单题到候选实测 8.42 秒（此前自动路径约 12.4–14.1 秒）。多题才继续详细定位；图片附带“按单题搜”等显式分流入口尚未接入。
- `requirements.txt` 没有版本约束，换机器安装存在依赖行为变化风险。
- 本地配置会覆盖代码默认值，调整默认参数时必须检查有效配置。
- PowerShell 写 JSON 可能产生 UTF-8 BOM，导致 Python `json.load(..., encoding="utf-8")` 失败。
- Windows 环境运行普通 Python 导入可能生成并锁定 `__pycache__`；项目验证优先使用 `python -B`，避免留下无法清理的 pyc 文件。
- 现有飞书运行进程可能尚未加载磁盘上的最新复筛代码，这是预期运行边界。

## Do Not Do

- 不提交、展示或写入 API key、token、密码和完整本地配置。
- 不回退用户已有改动，不把全局记忆写入项目文件。
- 不复用或覆盖现有飞书机器人的运行状态来开发 Agent。
- 不直接修改 live 题库数据；写操作必须确认并备份。
- 不让 LLM 直接用多题 bbox 做复筛；继续使用 OpenCV 图块流程并仅在不确定时调用模型。
- 不用复筛 80/90 输出规则改变粗筛候选池。
- 不把 `data/feishu_chapter_failure_log.jsonl` 等运行日志混入普通代码提交。

## Next Best Step

1. 增加图片附带自然语言的显式单题分流：用户明确要求单题时跳过首轮 scope，直接调用单题识别。
2. 用独立真实多题图完成 Agent 端到端回归，覆盖详细定位、裁图与无裁图回退。
3. 将复筛并发/超时参数配置化，增加同分候选确定性排序和 pipeline 集成测试。

## Important Commands

```powershell
python -B scripts/smoke_test.py
python -B -m unittest tests.test_tiku_agent_tools tests.test_tiku_agent_intent tests.test_tiku_agent_state tests.test_tiku_agent_agent
python -B scripts/search_by_loads.py --help
python -B search.py --help
python -B scripts/multi_agent_search.py --image "D:\path\to\question.jpg" --chapter auto
python -B scripts/feishu_tiku_bot.py dry-run-flow --image "D:\path\to\question.jpg" --chapter 5 --choice 1
```
