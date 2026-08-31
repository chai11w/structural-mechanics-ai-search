# Project Memory

## Current State

- A3-V1 已在 `8790` 生产：整页理解 → GLM 框选/Pillow 裁图 → Qwen 双门禁 → 单题自动下行或多题选择 → A2/人工裁剪；OpenCV/Paddle 不在主线。
- 端口隔离保持：8788 飞书、8790 生产、8795 后台、8896 验收当前监听；8794 无监听，8891/8897 为分流/回退端口。
- 8790 读取 8795 控制库认证；A1/A2/A3 与子 A2 统一计费。队列默认 1 个运行、2 个排队、55 秒等待，支持 FIFO、防插队、流关闭撤队和同锁分片去重。
- 8795 是可替换的过渡后台，Trace/Response 主链独立于它；8 个 live SQLite 库及 A2/A3、追问、反馈、费用和停用闭环已验收，4 条新反馈均绑定服务端 Response。
- 阶段 3.3、3.4 已完成并合入 `codex/mainline-bounded-autonomy-v1`，但尚未部署；3.5.1 离线回归与静态安全预检已在 `codex/task-state-stage-3-5-1` 完成，8896/live/8790 尚未进入。
- 当前验证：28 项前端聚焦、220 项本批次定向、73 项 task-state、13 项 watchdog 聚焦及全仓 1178 项全部通过；Node/Python/PowerShell 语法、依赖与差异检查通过。
- 已创建 3 个独立、7 天、每日 3 元模型估算额度的内测邀请码并验证；明文只经 8795 受控复制，不进入日志或项目文件。

## Implemented

- A3-V1 以 `unit_id` 绑定题目、框和裁图，多题并发校验上限 10；服务端校验状态、编号空间和 unit 集合。空分组简化或拒绝，schema 错误及媒体按既定期限过期。
- 共享复筛为综合分 `>=90%` 全部，否则可靠 Top 3；8896/V1+Qwen 为 `>=95%`。章节使用七存储键和三态解析，失败后仅经用户授权才搜字母库。
- 使用量按父 workflow 页数和实际进入 A2 的题数统计；费用归属稳定邀请码，父 A3/子 A2 共用估算账本。反馈 v8 绑定服务端 Response 并校验身份、会话、目标和有效期，v7 只读兼容。
- 公共五态协议只输出注册 code/白名单字段；候选原子交付，答案区分 0/部分/全部，媒体失败可重发，同题重试沿用 `search_id`。
- 8790 已实现有界队列、请求限长、登录限速、安全客户端地址和 JSON 工作线程；8795 管理登录同样限长限速，邀请码永久删除在费用库异常时 fail-closed。
- Trace/Response Store 使用白名单、唯一终态和幂等隐私投影；诊断 CLI 支持有界只读查询，retention 默认 dry-run。
- 8790/8795 看门狗精确核对端口、PID、Python 和完整参数；8896 还将绝对入口绑定 checkout、安全编码 argv，并在无法确认端口所有者时 fail-closed，三者均禁止按端口杀未知进程。
- 3.1～3.2 已冻结 `TaskStateSnapshotV1` 并实现纯构造、锁内单读、frozen-state 零重读及异常 fail-closed。
- 3.3～3.4 已统一 session/JSON/error/stream 快照；前端只由 branded `allowed_actions` 授权，动作绑定任务 identity/revision/target，拒绝 stale/ABA，网络未知结果不自动重放。
- 3.5.1 已完成：干净 worktree fixture、8896 精确进程管理和 external-load deadline 锁内失权均已回归；未运行服务/watchdog、访问端口、读取 live 数据或部署。

## In Progress

- 主阶段 3 仍进行中：3.1～3.4 DONE、已合入主线且尚未部署，3.5.1 DONE；下一独立批次为 3.5.2 8896 契约烟测。
- 本地内测发布已完成；待账户侧配置 Cloudflare Access 与边缘登录限速后，再向 2～3 名测试者发放邀请码并观察 24～48 小时。

## Not Implemented

- Cloudflare Access、边缘登录限速和测试者邮箱名单仍需账户侧配置；应用内限速不能替代边缘策略。
- 8795 尚无 Trace/Response 诊断 UI；未来若需要，只能作为可选只读消费者。
- 3.5.2～3.5.4 启用门与阶段 4～6 尚未实现。
- Paddle splitter、全自动裁剪及自动/人工回退属于 A3 V2，暂不继续。
- 8890 影子期费用报表、桁架高度几何计算、视觉重排和候选二次位置复筛仍未完成。
- retention 未安装周期调度、未真实 apply；运行日志仍只有 `policy_missing` 报告。

## Architecture Rules

- 8795 与 8790 保持独立；Trace/Response Store 和诊断查询独立于 8795，后者不是数据所有者。
- 管理员认证、Cookie、运行目录和控制数据不得与用户会话混用；8790 只读邀请码哈希，8795 加密保存新建或重置码。
- 控制库与 AES-GCM 密钥必须成对迁移和备份；迁移前核对 ID、哈希、状态和认证版本，冲突禁止写入。
- 费用归属稳定邀请码，不按临时 Cookie；预算准入前检查、完成后落账，保留单码额度和全站上限。
- 工具内部诊断与公共输出分层；新 Agent HTTP/Web 只接受注册错误码和白名单字段，个人飞书入口不纳入该边界。
- 8790/8896 的 A3-V1 固定为 GLM bbox + Pillow；OpenCV/Paddle 是遗留实验，不恢复为生产依赖。
- live 题库根为 `D:\桌面\答疑、帮做\结构力学\帮做`，字母库为相邻 `帮做_字母库`；仓库 Excel 是历史副本。
- 题库写操作必须 plan → confirm → backup → execute；服务端口、Cookie、状态、媒体和日志保持隔离。

## Known Risks

- Cloudflare Access 和边缘登录限速尚未从账户侧核验；完成前不应把公网地址和邀请码同时发给测试者。
- 真实烟测样本仍少；观察期需关注错绑、跨题费用归属、客户端时间异常、多题混排、裁剪边界、小荷载、低清和旋转。
- 主费用库 10 条早期 `glm-5v-turbo` 调用缺历史价格；原库保留 0 元，后台重估也不等同供应商实扣。
- Qwen 冷调用有长尾；1/2/55 队列保护额度，但第 4 个同时任务会直接繁忙，排队超过 55 秒需重试。
- 旧 `parse_chapter` 会把“第4章”映射为内部 `4力法`；严格入口对纯数字返回 `uncertain`，其他未迁移入口仍可能误搜。
- 邀请码转发会共享额度；签名 Cookie 不能阻止持码人主动共享，完成后落账也可能让最后一个在途任务略超阈值。
- Trace 写入为 fail-open，只以健康计数暴露丢失；WAL 双副本和只读检查降低风险，但不是绝对线性化快照。
- 全仓顺序回归曾因费用库 WAL checkpoint 使诊断测试观察到文件变化；单跑和复跑通过，仍属时序风险。
- 无 Web Lock 时自动过期 reset 与全部任务入口均在触网前 fail-closed，只允许 `/api/session`、`/api/reset` 对账；3.5 启用门需核验目标浏览器支持并给出升级提示。
- 8896 watchdog 已把绝对入口绑定脚本 checkout；3.5.2 仍须显式固定该 checkout 与绝对 runtime，不能把仓库 HEAD 当作运行版本证据。

## Do Not Do

- 不读取、提交或展示 API key、Tunnel token、邀请码明文、管理员密码、私有发放清单或本地敏感配置。
- 不把管理员认证并入用户会话，不把 8795 部署进 8790，也不让 8795 成为 Trace/Response 所有者。
- 不因后台 ID/哈希一致就假定旧邀请码可用；灾备还必须核对状态、登录和动态撤销。
- 不把邀请码身份改回会话 Cookie，不删除全站保险上限。
- 不跨章节搜索，不绕过项目脚本识别、过滤和排序；未授权时不把图片发给外部模型。
- 不把公共输出改造扩展到个人飞书入口，不随意停止 8788。
- 不按端口批量杀进程，不覆盖活 PID 文件；身份核对失败时停在现场。
- 未经用户另行确认，不访问、部署或操作 8790；3.5 不触碰 8788、8794、8795，也不调用会读取这些运行状态的旧切换脚本。
- 不在目标回复缺失时保存整段反馈历史，也不把反馈专用框选图重复注入普通聊天消息。

## Next Best Step

1. 用户确认后执行 3.5.2：只在 8896 做 task-state 契约烟测；先固定代码 checkout 与绝对 runtime，使用已硬化的 8896 watchdog，不触碰 8788/8790/8794/8795。
2. 3.5.2 通过后单独汇报；3.5.3/3.5.4 仍须再次确认，只能从 8790 侧静态证据核对 control 引用，不连接、读取或探测 8795；若无法在该边界内证明或启用则阻断并重新确认。
3. 账户侧为 8790/8795 配置独立 Cloudflare Access 与窄范围边缘限速后，再受控发放 3 个邀请码并观察 24～48 小时。

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
