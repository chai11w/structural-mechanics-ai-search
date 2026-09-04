# Project Memory

## Current State

- A3-V1 已在 `8790` 生产：Qwen 整页理解 → GLM 框选/Pillow 裁图 → Qwen 双门禁 → 单题自动下行或多题选择 → A2/人工裁剪；8790、8896 和飞书 8788 储存入口均已停止四方向 RapidOCR 校正，RapidOrientation 尚未接入。
- 端口隔离保持：8788 飞书、8790 生产、8795 后台；8896 的 `answer-session-v9` 已修复候选答案误拒绝及后续拖图退回首页，全仓 1236 项、内置浏览器连续两题及刷新恢复通过。
- 8790 读取 8795 控制库认证；A1/A2/A3 与子 A2 统一计费。队列默认 1 个运行、2 个排队、55 秒等待，支持 FIFO、防插队、流关闭撤队和同锁分片去重。
- 8795 是可替换的过渡后台，Trace/Response 主链独立；8 个 live SQLite 库及反馈、费用、停用闭环已验收。
- 8790 已于 2026-09-04 启用主线 `fbfcf435` 固定 release；进程链、唯一 listener、健康/Trace、v9 哈希和生产认证均通过。回退包 `8790-answer-session-v9-20260904-100124` 含旧任务 XML、Git bundle 和 9 份 SQLite 副本。
- 已创建 3 个独立、7 天、每日 3 元模型估算额度的内测邀请码并验证；明文只经 8795 受控复制，不进入日志或项目文件。

## Implemented

- A3-V1 以 `unit_id` 绑定题目、框和裁图，多题校验上限 10；服务端校验状态、编号和 unit 集合，媒体按期过期。
- 共享复筛为综合分 `>=90%` 全部，否则 Top 3；8896 为 `>=95%`。章节解析失败后仅经授权搜字母库。
- 使用量按父 workflow 页数和进入 A2 的题数统计；费用归属稳定邀请码，A3/子 A2 共用账本。反馈 v8 绑定服务端 Response，v7 只读兼容。
- 公共五态协议只输出注册 code/白名单字段；候选原子交付，媒体失败可重发，同题重试沿用 `search_id`。
- 8790 已实现有界队列、请求限长、登录限速和 JSON 工作线程；8795 管理登录同样限长限速，费用库异常时邀请码删除 fail-closed。
- Trace/Response Store 使用白名单、唯一终态和幂等隐私投影；诊断 CLI 只读，retention 默认 dry-run。
- 8790/8795 看门狗核对端口、PID、Python 和完整参数；8790/8896 还绑定绝对 checkout/入口、安全 argv，端口所有者不明时 fail-closed。8790 每次启动复验 manifest、完整提交、干净 linked checkout、Python/runtime；Limited 任务以显式 UTF-8 和 `core.quotePath=false` 解析中文 Git 路径。
- 3.1～3.2 已冻结 `TaskStateSnapshotV1` 并实现纯构造、锁内单读、frozen-state 零重读及异常 fail-closed。
- 3.3～3.4 已统一 session/JSON/error/stream 快照；前端只由 branded `allowed_actions` 授权，动作绑定任务 identity/revision/target，拒绝 stale/ABA，网络未知结果不自动重放。
- 3.5.1～3.5.4 已完成：3.5.3 因旧启动链无可信 release/回退锚点正确 BLOCKED，3.5.4 建立固定 release 并精确启用。refresh-recovery 已实现 `/api/session` 单一对账、超时和 pending fence；8896 进一步允许严格限定的答案完成历史态，并在新出现 fence 时补偿对账，不改 V1、不开始阶段 4。

## In Progress

- 8790 `answer-session-v9` 进入试用；新内置浏览器无生产 Cookie，需在登录浏览器完成连续两题与刷新恢复。
- 方向评估未实现：已提交 9×4 基线为 RapidOrientation/PP-LCNet `33/36`；主工作区未提交 frontdoor 6×4 与 evaluator/manifest，2 项测试通过但 `real_cases` 为空。OCR `20/24`、Rapid `17/24`、106→424 无安全阈值仅有本地记录，尚未复核。
- 主工作区落后远端且脏；旧 `fence-login-v9` 变体会移除最终补偿和无 Cookie 测试，不得并入。`a3_routing_baseline/` 15 图为重复数据；治理前禁止 pull/reset 或删除。
- 远端主线在 8790 release 后新增可比单尺寸硬过滤及测试，尚未部署 8790，不能视作当前生产行为。
- 本地内测发布和主阶段 3 已完成；待账户侧配置 Cloudflare Access 与边缘登录限速后，再向 2～3 名测试者发放邀请码并观察 24～48 小时。

## Not Implemented

- Cloudflare Access、边缘登录限速和测试者邮箱名单仍需账户侧配置；应用内限速不能替代边缘策略。
- 阶段 4～6 尚未实现；不要因主阶段 3 上线而自动开始。
- 仓库尚无可复用的 8790 计划任务 release 发布器；`switch_tiku_agent_8790_control.ps1` 只用于早期控制库迁移，不承担任务 XML/action 切换与代码回退。
- RapidOrientation 封装、阈值、8896 影子和 8790 发布未实现；需提取 ONNX 置信度并固定版本/模型哈希。
- Paddle splitter、全自动裁剪及自动/人工回退属于 A3 V2，暂不继续。
- retention 未安装周期调度、未真实 apply；运行日志仍只有 `policy_missing` 报告。

## Architecture Rules

- 8795 与 8790 保持独立；Trace/Response Store 和诊断查询独立于 8795，后者不是数据所有者。
- 管理员认证、Cookie、运行目录和控制数据不得与用户会话混用；8790 只读邀请码哈希，8795 加密保存新建或重置码。
- 控制库与 AES-GCM 密钥必须成对迁移和备份；迁移前核对 ID、哈希、状态和认证版本，冲突禁止写入。
- 费用归属稳定邀请码，不按临时 Cookie；预算准入前检查、完成后落账，保留单码额度和全站上限。
- 工具内部诊断与公共输出分层；新 Agent HTTP/Web 只接受注册错误码和白名单字段，个人飞书入口不纳入该边界。
- A3 裁剪固定为 GLM bbox + Pillow；若恢复方向预处理，优先独立评估 ONNX RapidOrientation，不恢复 Paddle 主链或默认四方向 OCR。
- live 题库根为 `D:\桌面\答疑、帮做\结构力学\帮做`，字母库为相邻 `帮做_字母库`；仓库 Excel 是历史副本。
- 题库写操作必须 plan → confirm → backup → execute；服务端口、Cookie、状态、媒体和日志保持隔离。

## Known Risks

- Cloudflare Access 和边缘登录限速尚未从账户侧核验；完成前不应把公网地址和邀请码同时发给测试者。
- 真实烟测样本仍少；观察期需关注错绑、跨题费用归属、客户端时间异常、多题混排、裁剪边界、小荷载、低清和旋转。
- 方向阈值未校准；现有阈值无法同时保证误旋安全和召回，不能直接上线。
- Qwen 冷调用有长尾；1/2/55 队列保护额度，但第 4 个同时任务会直接繁忙，排队超过 55 秒需重试。
- 旧 `parse_chapter` 会把“第4章”映射为内部 `4力法`；严格入口对纯数字返回 `uncertain`，其他未迁移入口仍可能误搜。
- 邀请码转发会共享额度；完成后落账可能让最后一个在途任务略超阈值。
- Trace 写入为 fail-open，只以健康计数暴露丢失；WAL 双副本和只读检查降低风险，但不是绝对线性化快照。
- 无 Web Lock 时任务入口 fail-closed，只允许会话对账；8896 浏览器完整路径已通过，发放前仍需覆盖测试者浏览器。
- NATAPP 静态资源和健康可达不证明公网登录恢复闭环。
- 8790 冷启动约 15～18 秒；须等待 PID 链稳定并跨 watchdog 周期复核，回退只停已捕获 PID，禁止扫描命令行。

## Do Not Do

- 不读取、提交或展示 API key、Tunnel token、邀请码明文、管理员密码、私有发放清单或本地敏感配置。
- 不把管理员认证并入用户会话，不把 8795 部署进 8790，也不让 8795 成为 Trace/Response 所有者。
- 不因后台 ID/哈希一致就假定旧邀请码可用；灾备还必须核对状态、登录和动态撤销。
- 不把邀请码身份改回会话 Cookie，不删除全站保险上限。
- 不跨章节搜索，不绕过项目脚本识别、过滤和排序；未授权时不把图片发给外部模型。
- 无新证据时不重新默认开启四方向 OCR，也不把 RapidOrientation 当作已验证替代。
- 不把公共输出改造扩展到个人飞书入口，不随意停止 8788。
- 不按端口批量杀进程，不覆盖活 PID 文件；身份核对失败时停在现场。
- 8790 发布必须固定 release/manifest、备份数据与任务 XML，并按完整身份精确切换；不得用控制库迁移脚本充当发布器或影响 8788/8794/8795。
- 不读或操作 8888；它与 8790 无关。未经用户明确授权不改或重启 NATAPP。
- 不在目标回复缺失时保存整段反馈历史，也不把反馈专用框选图重复注入普通聊天消息。

## Next Best Step

1. 在已有登录 Cookie 的浏览器实际使用 8790，完成连续两题、答案返回和刷新恢复；若再出现会话提示，保留页面、操作顺序和题图，先停止继续操作并对照 8896 定位。
2. 先清点主工作区并在新隔离 worktree 只移植方向评估，排除旧会话变体和重复测试图；未经逐项确认不删除，完成前不开始阶段 4。
3. 账户侧配置 8790/8795 的 Cloudflare Access 与边缘限速后，再受控发放邀请码。

## Important Commands

- `python -m unittest discover -s tests -p 'test_*.py'`
- `python -m unittest discover -v -s tests -p 'test_task_state_*.py'`
- `python -m unittest -v tests.test_tiku_agent_fastapi_demo tests.test_a3_runtime tests.test_a3_web_ui_copy tests.test_demo_web_task_state tests.test_task_state_exit_parity`
- `python -m unittest -v tests.test_tiku_agent_watchdog_8896 tests.test_tiku_agent_watchdog_8790 tests.test_watchdog_process_guard`
- `python scripts/run_tiku_agent_8790.py --help`
- `python scripts/tiku_diagnostics.py --help`
- `python scripts/tiku_retention.py --help`
- `python scripts/search_by_loads.py --help`
- `python search.py --help`
