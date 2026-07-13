# Project Memory

## Current State

- 项目位于 `F:\cc\7-题库检索`，用于维护结构力学题库并按题图或荷载检索相似题、返回答案。
- CLI、现有飞书检索/入库/删除、章节 2-8、主库/字母库分流和字母库结构类型筛选可用。
- 独立 `tiku_agent/` 已完成工具层、LLM intent、对话状态和单题/多题 MVP；LangGraph checkpoint 与新飞书机器人尚未接入。
- Agent 使用 `.tmp_tiku_agent`，现有飞书使用 `.tmp_feishu_tiku`；两套入口和运行状态必须持续隔离。
- FastAPI Demo 监听 `127.0.0.1:8790`，通过独立 Cloudflare Tunnel 和稳定公网域名开放；电脑、Demo 和 tunnel 在线时公网可用。
- Demo 当前代码内容已恢复到首次访问即建立会话、刷新可恢复题图的稳定版本；后来增加的微信裁剪/HEIC 兼容已整体撤销。
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
- Demo 已具备单会话聊天画布、移动端菜单、拖放上传、候选选择、大图预览、固定顶栏/输入区、中文安全错误和请求取消。
- 公网入口已关闭 OpenAPI，启用 HTTPS 重定向、安全 Cookie、CSP 与基础安全响应头，并展示云端识别和敏感信息提示。
- 章节识别先提取 `visible_problem_text`：没有实际题干则 `unknown`；有题干才结合题干和题型判断章节。
- 赋值符号荷载统一为无单位 `符号=数值`，进入主库；纯符号进入字母库。
- 视觉复筛使用 GLM shape-only；只比较主杆件骨架与轮廓，不看荷载、文字、尺寸和支座细节。
- 最终分为粗筛荷载分与视觉轮廓分各 50%；90 分以上全显，否则只显示最高分，CLI、飞书与 Agent 共用策略。
- 并发复筛最多 10 个候选；首轮单候选 8 秒，最多补评 3 个、每项 10 秒；批次不完整则整批回退粗筛排序。

## In Progress

- 当前没有未完成代码改动；下一阶段原计划是增强 intent 的隐私受限上下文和多轮指代推理。

## Not Implemented

- 尚未引入 LangGraph 图或 checkpoint，也未创建、连接新的飞书机器人。
- 复筛并发数、首轮超时和补评上限尚未配置化。
- 缺少 pipeline 级超时回退、真实飞书事件和持久状态集成测试。
- 微信内置浏览器裁剪后的临时图片兼容尚未实现；现有前端仍同时严格检查 MIME 与扩展名，空 MIME、`application/octet-stream`、HEIC/HEIF 可能被拒绝。
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

## Known Risks

- 微信/荣耀裁剪文件的实际 `File.name`、`File.type` 和原始字节尚未采样确认；不要仅凭错误提示假定一定是 HEIC。
- Intent 获得的会话上下文有限，“另一个/刚才那个/剩下那道”等指代仍容易误判。
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
- 未拿到微信裁剪后的真实元数据或原始文件前，不重新套用已撤销的全格式规范化方案。

## Next Best Step

1. 若继续解决微信裁剪问题，先临时采集裁剪文件的 `name/type/size`，并取得一份裁剪后的原始文件；确认根因后再设计最小兼容修复。
2. 若回到 Agent 主线，为 intent 增加隐私受限会话摘要与受约束动作推理，并用多轮指代样本做新旧对照测试。
3. 面试演示前从电脑浏览器和微信各跑一遍上传、刷新恢复、候选选择和答案返回，不再临时扩大改动范围。

## Important Commands

```powershell
python -B -m unittest discover -s tests -p "test_*.py"
python -B scripts/smoke_test.py
python -B scripts/search_by_loads.py --help
python -B search.py --help
python -B -m uvicorn tiku_agent.fastapi_demo:create_app --factory --host 127.0.0.1 --port 8790
Invoke-RestMethod http://127.0.0.1:8790/health
```
