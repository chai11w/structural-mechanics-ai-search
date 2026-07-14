# Project Memory

## Current State

- 项目位于 `F:\cc\7-题库检索`，用于维护结构力学题库并按题图或荷载检索相似题、返回答案。
- CLI、现有飞书检索/入库/删除、章节 2-8、主库/字母库分流和字母库结构类型筛选可用。
- 独立 `tiku_agent/` 已完成工具层、LLM intent、对话状态和单题/多题 MVP；LangGraph checkpoint 与新飞书机器人尚未接入。
- MVP 后的当前阶段已确定为 Intent V2：先提高多轮指代、上下文动作判断和安全追问的可靠性，不直接重构 LangGraph。
- Agent 使用 `.tmp_tiku_agent`，现有飞书使用 `.tmp_feishu_tiku`；两套入口和运行状态必须持续隔离。
- FastAPI Demo 监听 `127.0.0.1:8790`，题库飞书监听 `127.0.0.1:8788`；两者使用固定 Cloudflare Tunnel，已配置 Windows 登录自启，tunnel 系统服务为自动启动。
- Demo 保留首次访问建会话和刷新恢复题图，并已修复微信裁剪产物缺少文件名/MIME 时的上传链路；HEIC/HEIF 仍不在支持范围。
- 当前分支为 `codex/llm-rerank-support-filter`；`data/feishu_chapter_failure_log.jsonl` 有用户运行日志改动，禁止混入普通提交。

## Implemented

- `search.py`：粗筛、并发视觉复筛、答案定位和基础 CLI；默认 `top_k=3`，本地配置可覆盖。
- `multi_agent_pipeline.py`：题图识别、章节判断、主库/字母库路由、结构类型筛选和复筛协调。
- `scripts/feishu_tiku_bot.py`：现有飞书单题/多题检索、章节选择、答案返回、入库和候选删除。
- `scripts/feishu_store_flow.py` 与 `scripts/feishu_delete_flow.py`：题库写操作采用计划、确认和备份。
- `tiku_agent/tools.py`、`state.py`、`agent.py`、`render.py`：隔离工具、状态和自然语言编排；支持章节纠正、多题选题、候选切换和答案回看。
- Intent 采用少量确定性规则、Qwen 动作判断、状态校验和规则降级；明确章节优先于模型结果。
- SQLite 会话与 session 文件持久化已实现；最后活跃 2 小时过期，取消清理状态与临时文件。
- 页面首次打开即创建会话 Cookie；首次上传后立即退出再进入、刷新或 Demo 重启时，两小时内可恢复会话绑定题图 URL。
- Demo 已具备单会话聊天画布、移动端菜单、拖放上传、候选选择、大图预览、固定顶栏/输入区、中文安全错误和请求取消；选图结果统一经 canvas 输出质量 0.92 的 JPEG Blob，以 `file` multipart 字段上传，失败保留预览和 Blob 可直接重试，成功响应后才显示用户题图。
- 公网入口已关闭 OpenAPI，启用 HTTPS 重定向、安全 Cookie、CSP 与基础安全响应头，并展示云端识别和敏感信息提示。
- 章节识别先提取 `visible_problem_text`：没有实际题干则 `unknown`；有题干才结合题干和题型判断章节。
- 赋值符号荷载统一为无单位 `符号=数值`，进入主库；纯符号进入字母库。
- 视觉复筛使用 GLM shape-only；只比较主杆件骨架与轮廓，不看荷载、文字、尺寸和支座细节。
- 最终分为粗筛荷载分与视觉轮廓分各 50%；90 分以上全显，否则只显示最高分，CLI、飞书与 Agent 共用策略。
- 并发复筛最多 10 个候选；首轮单候选 8 秒，最多补评 3 个、每项 10 秒；批次不完整时回退粗筛，有 100% 粗筛候选则全部展示，否则只展示粗筛第一。

## In Progress

- Intent V2 已完成方案审查和 8 步动作拆解，尚未开始代码修改；详细顺序与验收门槛见 `.agents/roadmap.md`。
- 当前工作树没有 Intent V2 半成品；下一步先冻结 V1 基线，再定义 ActionDecision V2 和状态—动作权限矩阵。

## Not Implemented

- 尚未引入 LangGraph 图或 checkpoint，也未创建、连接新的飞书机器人。
- 尚未实现 ConversationContext V2、题目/候选编号命名空间、结构化动作协议、歧义追问和 V1/V2 影子对照。
- 尚未建立约 40 组真实多轮意图评测集；现有测试覆盖规则和状态转换，但不足以衡量“另一个/刚才那个/剩下那题”等连续指代。
- 复筛并发数、首轮超时和补评上限尚未配置化。
- 缺少 pipeline 级超时回退、真实飞书事件和持久状态集成测试。
- HEIC/HEIF 浏览器解码兼容尚未实现；当前支持 JPEG、PNG、WEBP、GIF、BMP，空 MIME 或 `application/octet-stream` 会先尝试按真实图片字节解码。
- 公网访问次数限制、身份认证和滥用防护尚未启用。

## Architecture Rules

- 新 Agent 必须使用独立入口、配置、机器人凭据、端口、隧道、session、日志、checkpoint 和临时文件。
- 不停止、重启或改造现有 `scripts/feishu_tiku_bot.py` 来测试 Agent；Demo 仅使用独立 8790 入口。
- 早期 Agent 可以只读 live 题库；入库、删除和修复必须走 `plan -> confirm -> execute` 并备份。
- 章节只能由用户指定/确认，或由题干文字证据自动确定；纯结构图不能跨章节猜测。
- 荷载类型只用 `集中`、`均布`、`弯矩`；`raw` 保留原始标注并做单位归一化。
- 主库保存数值荷载和已赋值符号题，字母库保存未赋值字母题；只有字母库图片检索使用结构类型筛选。
- 复筛只处理粗筛候选，不改变候选池；未完成批次不能混合视觉分排序。
- 修改共享检索、储存、索引或 Agent 逻辑时，同步检查 CLI、飞书、Agent 和项目 Skill 文档。
- Intent V2 每个用户回合最多执行一个高层动作；LLM 负责理解，代码负责状态权限、参数范围和禁止动作校验，不开放自由多工具循环。
- Intent 上下文只提供完成判断所需的会话摘要，不暴露本地绝对路径、凭据、完整模型原始输出或无关题库数据。

## Known Risks

- 微信/荣耀真机裁剪文件的实际 `File.name`、`File.type` 和原始字节仍未采样；当前以 Chromium 模拟无后缀、通用 MIME 的裁剪产物验证通过，但真机 canvas 内存、EXIF 方向和 HEIC 解码仍需实测。
- Intent 获得的会话上下文有限，“另一个/刚才那个/剩下那道”等指代仍容易误判。
- 当前模型只看到 phase、题目数、候选数和本轮文本；`AgentState` 中已有的题目、候选、章节和最近动作尚未形成可校验的上下文摘要。
- 同粗筛分的超时候选补评顺序可能受线程完成顺序影响，尚无确定性次序。
- `search.py` 和现有飞书脚本体量较大，章节及中文序号解析存在行为漂移风险。
- 单元测试以 mock/纯函数为主，外部模型、飞书事件与持久状态覆盖不足。
- 公网 Demo 依赖本机和 Cloudflare Tunnel 持续在线；目前没有访问配额，可能产生模型费用。
- `requirements.txt` 没有完整版本约束，换机器安装可能出现依赖行为变化。
- 本地配置会覆盖代码默认值；调整默认参数时必须检查有效配置。
- Windows 验证优先使用 `python -B`，避免生成并锁定 `__pycache__`。

## Do Not Do

- 不提交、展示或写入 API key、token、密码和完整本地配置。
- 不回退用户已有改动，不把全局记忆写入项目文件。
- 不复用现有飞书机器人的运行状态开发 Agent，也不随意重启现有飞书服务。
- 不直接修改 live 题库；写操作必须确认并备份。
- 不让 LLM 直接用多题 bbox 复筛；继续使用 OpenCV 图块流程。
- 不让复筛展示规则改变粗筛候选池。
- 不提交 `data/feishu_chapter_failure_log.jsonl` 等运行日志。
- 未拿到真实 HEIC/HEIF 样本前，不扩展全格式服务端转码或引入 HEIF 依赖；当前客户端只规范化浏览器能解码的支持格式。

## Next Best Step

1. 冻结 Intent V1 行为：检查 Git、运行现有测试并记录当前输入、动作输出和已知失败样本。
2. 先设计 ActionDecision V2 与状态—动作权限矩阵，再据此建立约 40 组真实多轮评测，不从改 Prompt 开始。
3. 评测基线完成后实现 ConversationContext V2、受约束 Qwen 动作判断和歧义追问；LangGraph 留到动作协议稳定后再评估。

## Important Commands

```powershell
python -B -m unittest discover -s tests -p "test_*.py"
python -B -m unittest tests.test_tiku_agent_intent tests.test_tiku_agent_agent tests.test_tiku_agent_session_runtime
python -B scripts/smoke_test.py
python -B scripts/search_by_loads.py --help
python -B search.py --help
python -B -m uvicorn tiku_agent.fastapi_demo:create_app --factory --host 127.0.0.1 --port 8790
Invoke-RestMethod http://127.0.0.1:8790/health
```
