# Project Memory

## Current State

- A3-V1 已在 `8790` 生产：Qwen 整页理解 → GLM 框选/Pillow 裁图 → Qwen 双门禁 → 单题自动下行或多题选择 → A2/人工裁剪；8790、8896 和飞书 8788 储存入口均已停止四方向 RapidOCR 校正，RapidOrientation 尚未接入。
- 端口隔离保持：8788 飞书、8790 生产、8795 后台；8896 运行 `answer-session-v9` 固定 release，候选答案误拒绝和后续拖图退回首页均已修复。全仓 1236 项、内置浏览器连续两题及刷新恢复通过；本轮未动 8790/8888。
- 8790 读取 8795 控制库认证；A1/A2/A3 与子 A2 统一计费。队列默认 1 个运行、2 个排队、55 秒等待，支持 FIFO、防插队、流关闭撤队和同锁分片去重。
- 8795 是可替换的过渡后台，Trace/Response 主链独立；8 个 live SQLite 库及反馈、费用、停用闭环已验收。
- 主阶段 3 已完成：3.3/3.4 的统一任务状态出口与前端消费已随 3.5.4 启用到 8790；生产计划任务固定到受 manifest 约束的干净 release checkout，不再从可变主工作区启动。
- 8790 为 refresh-recovery 固定 release，全仓 1222 项通过；公网静态资源和 `/health` 一致，登录 `/api/session` 对账仍待闭环。3.5.3 DONE/BLOCKED、3.5.4 DONE 的历史状态不可改写。
- 已创建 3 个独立、7 天、每日 3 元模型估算额度的内测邀请码并验证；明文只经 8795 受控复制，不进入日志或项目文件。

## Implemented

- A3-V1 以 `unit_id` 绑定题目、框和裁图，多题校验上限 10；服务端校验状态、编号和 unit 集合，媒体按期过期。
- 共享复筛为综合分 `>=90%` 全部，否则可靠 Top 3；8896/V1+Qwen 为 `>=95%`。章节使用七存储键和三态解析，失败后仅经用户授权才搜字母库。
- 使用量按父 workflow 页数和进入 A2 的题数统计；费用归属稳定邀请码，A3/子 A2 共用账本。反馈 v8 绑定服务端 Response，v7 只读兼容。
- 公共五态协议只输出注册 code/白名单字段；候选原子交付，媒体失败可重发，同题重试沿用 `search_id`。
- 8790 已实现有界队列、请求限长、登录限速和 JSON 工作线程；8795 管理登录同样限长限速，费用库异常时邀请码删除 fail-closed。
- Trace/Response Store 使用白名单、唯一终态和幂等隐私投影；诊断 CLI 只读，retention 默认 dry-run。
- 8790/8795 看门狗核对端口、PID、Python 和完整参数；8790/8896 还绑定绝对 checkout/入口、安全 argv，端口所有者不明时 fail-closed。8790 每次启动复验 manifest、完整提交、干净 linked checkout、Python/runtime；Limited 任务以显式 UTF-8 和 `core.quotePath=false` 解析中文 Git 路径。
- 3.1～3.2 已冻结 `TaskStateSnapshotV1` 并实现纯构造、锁内单读、frozen-state 零重读及异常 fail-closed。
- 3.3～3.4 已统一 session/JSON/error/stream 快照；前端只由 branded `allowed_actions` 授权，动作绑定任务 identity/revision/target，拒绝 stale/ABA，网络未知结果不自动重放。
- 3.5.1～3.5.4 已完成：3.5.3 因旧启动链无可信 release/回退锚点正确 BLOCKED，3.5.4 建立固定 release 并精确启用。refresh-recovery 已实现 `/api/session` 单一对账、超时和 pending fence；8896 进一步允许严格限定的答案完成历史态，并在新出现 fence 时补偿对账，不改 V1、不开始阶段 4。

## In Progress

- 8790 refresh-recovery 继续实际试用；已登录浏览器的 `/api/session` 可重复恢复验收仍未闭环，不以 8896 验收通过替代 8790 live acceptance。
- 方向选型完成、实现未开始：9 张 A3 × 4 方向中，RapidOrientation 0.0.11 与 PP-LCNet 均为 `33/36`，都败在 `6.jpg`；前者约 `0.017～0.021s/张`。
- 本地内测发布和主阶段 3 已完成；待账户侧配置 Cloudflare Access 与边缘登录限速后，再向 2～3 名测试者发放邀请码并观察 24～48 小时。

## Not Implemented

- Cloudflare Access、边缘登录限速和测试者邮箱名单仍需账户侧配置；应用内限速不能替代边缘策略。
- 8795 尚无 Trace/Response 诊断 UI；未来若需要，只能作为可选只读消费者。
- 阶段 4～6 尚未实现；不要因主阶段 3 上线而自动开始。
- 仓库尚无可复用的 8790 计划任务 release 发布器；`switch_tiku_agent_8790_control.ps1` 只用于早期控制库迁移，不承担任务 XML/action 切换与代码回退。
- RapidOrientation 封装、阈值、8896 影子和 8790 发布未实现；需提取 ONNX 置信度并固定版本/模型哈希。
- Paddle splitter、全自动裁剪及自动/人工回退属于 A3 V2，暂不继续。
- 8890 影子期费用报表、桁架高度几何计算、视觉重排和候选二次位置复筛仍未完成。
- retention 未安装周期调度、未真实 apply；运行日志仍只有 `policy_missing` 报告。

## Architecture Rules

- 8795 与 8790 保持独立；Trace/Response Store 和诊断查询独立于 8795，后者不是数据所有者。
- 管理员认证、Cookie、运行目录和控制数据不得与用户会话混用；8790 只读邀请码哈希，8795 加密保存新建或重置码。
- 控制库与 AES-GCM 密钥必须成对迁移和备份；迁移前核对 ID、哈希、状态和认证版本，冲突禁止写入。
- 费用归属稳定邀请码，不按临时 Cookie；预算准入前检查、完成后落账，保留单码额度和全站上限。
- 工具内部诊断与公共输出分层；新 Agent HTTP/Web 只接受注册错误码和白名单字段，个人飞书入口不纳入该边界。
- A3 裁剪固定为 GLM bbox + Pillow；方向预处理独立使用 ONNX RapidOrientation，不恢复 Paddle 主链或默认四方向 OCR。
- live 题库根为 `D:\桌面\答疑、帮做\结构力学\帮做`，字母库为相邻 `帮做_字母库`；仓库 Excel 是历史副本。
- 题库写操作必须 plan → confirm → backup → execute；服务端口、Cookie、状态、媒体和日志保持隔离。

## Known Risks

- Cloudflare Access 和边缘登录限速尚未从账户侧核验；完成前不应把公网地址和邀请码同时发给测试者。
- 真实烟测样本仍少；观察期需关注错绑、跨题费用归属、客户端时间异常、多题混排、裁剪边界、小荷载、低清和旋转。
- 主费用库 10 条早期 `glm-5v-turbo` 调用缺历史价格；原库保留 0 元。
- 方向阈值未校准；现有阈值无法同时保证误旋安全和召回，不能直接上线。
- Qwen 冷调用有长尾；1/2/55 队列保护额度，但第 4 个同时任务会直接繁忙，排队超过 55 秒需重试。
- 旧 `parse_chapter` 会把“第4章”映射为内部 `4力法`；严格入口对纯数字返回 `uncertain`，其他未迁移入口仍可能误搜。
- 邀请码转发会共享额度；完成后落账可能让最后一个在途任务略超阈值。
- Trace 写入为 fail-open，只以健康计数暴露丢失；WAL 双副本和只读检查降低风险，但不是绝对线性化快照。
- 全仓顺序回归曾因费用库 WAL checkpoint 使诊断测试观察到文件变化；单跑和复跑通过，仍属时序风险。
- 无 Web Lock 时自动过期 reset 与全部任务入口均在触网前 fail-closed，只允许 `/api/session`、`/api/reset` 对账；8896 的 Chromium 与 Codex 内置浏览器完整路径已通过，发放前仍需覆盖实际测试者浏览器。
- NATAPP 静态资源和 `/health` 可达不证明登录恢复闭环；当前证据无法定位到 NATAPP、Web Lock 或 8790 中的某一层。

## Do Not Do

- 不读取、提交或展示 API key、Tunnel token、邀请码明文、管理员密码、私有发放清单或本地敏感配置。
- 不把管理员认证并入用户会话，不把 8795 部署进 8790，也不让 8795 成为 Trace/Response 所有者。
- 不因后台 ID/哈希一致就假定旧邀请码可用；灾备还必须核对状态、登录和动态撤销。
- 不把邀请码身份改回会话 Cookie，不删除全站保险上限。
- 不跨章节搜索，不绕过项目脚本识别、过滤和排序；未授权时不把图片发给外部模型。
- 不把公共输出改造扩展到个人飞书入口，不随意停止 8788。
- 不按端口批量杀进程，不覆盖活 PID 文件；身份核对失败时停在现场。
- 8790 发布必须固定 release/manifest、备份数据与任务 XML，并按完整身份精确切换；不得用控制库迁移脚本充当发布器或影响 8788/8794/8795。
- 不读或操作 8888；它与 8790 无关。未经用户明确授权不改或重启 NATAPP。
- 不在目标回复缺失时保存整段反馈历史，也不把反馈专用框选图重复注入普通聊天消息。

## Next Best Step

1. 继续在 8896 实际试用；若再次出现会话提示，保留当时页面、操作顺序和题图，先在 8896 定位，稳定后再另行决定是否发布到 8790。
2. 账户侧为 8790/8795 配置独立 Cloudflare Access 与窄范围边缘限速后，再受控发放 3 个邀请码。
3. 根据真实失败记录审查 RapidOrientation 接入或阶段 4；未经单独确认不开始阶段 4。

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
