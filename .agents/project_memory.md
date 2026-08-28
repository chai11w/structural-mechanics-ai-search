# Project Memory

## Current State

- A3-V1 已在 `8790` 生产：Qwen 整页理解 → GLM 全题框选 → Pillow 裁图 → Qwen 并发完整性/外荷载门禁 → 单题自动下行或多题选择 → A2/人工裁剪；OpenCV/Paddle 不在主线启动链。
- 端口隔离保持：8788 飞书、8790 生产、8795 后台、8896 验收当前监听；8794 无监听，8891/8897 为分流/回退端口。
- 8790 读取 8795 控制库认证；A1/A2/A3 与子 A2 统一计费。队列默认 1 个运行、2 个排队、55 秒等待，支持 FIFO、防插队、流关闭撤队和同锁分片去重。
- 8795 是可替换的过渡后台；Trace/Response 主链独立于它。live 已核对 Response、反馈、费用、Trace、身份与时间顺序，未增加 Trace 页面。
- 8 个 live SQLite 库在发布前后均通过 `quick_check`、`integrity_check`；A2、A3、追问、反馈、费用展示和停用烟测通过，4 条新反馈均绑定服务端 Response。
- 阶段 3.3.3 已在代码中完成但未部署：受控 HTTP 200 业务 JSON 返回根级 `task_state`，HTTP error、stream、前端和 8790 启用均未进入本批。
- 当前开发基线：65 项 task-state、193 项相关出口及全仓 1072 项测试全部通过。
- 已创建 3 个独立、7 天、每日 3 元模型估算额度的内测邀请码并验证；明文只经 8795 受控复制，不进入日志或项目文件。

## Implemented

- A3-V1 以 `unit_id` 绑定题目、框和裁图，多题并发校验上限 10；服务端校验状态、编号空间和原样 unit 集合，防止重复搜索或完成。
- 空分组按引用关系简化或拒绝；schema 错误只存 code/简因 30 天，题图、裁图、候选和答案在新对话或 2 小时后过期。
- 共享复筛为综合分 `>=90%` 全部，否则可靠 Top 3；8896/V1+Qwen 为 `>=95%`。章节使用七存储键和三态解析，失败后仅经用户授权才搜字母库。
- 今日使用按父 workflow 统计页数、按进入 A2 的题目统计检索题数；费用归属稳定邀请码，父 A3/子 A2 共用账本。费用为 Token 与版本价目表估算，不等同供应商实扣。
- 反馈 schema v8 校验 `rated_response_id`、conversation、identity/session 与有效期；旧 v7 只读兼容。A3 反馈使用目标 Response 的服务端投影。
- 公共五态协议只输出注册 code/白名单字段；候选原子交付，答案区分 0/部分/全部，媒体失败可重发，同题重试沿用 `search_id`。
- 8790 已实现有界队列、请求体限长、邀请码登录限速、安全客户端地址和 JSON 长调用工作线程；8795 管理登录同样限长限速，邀请码永久删除在费用库异常时 fail-closed。
- Trace Store 使用严格白名单、唯一终态和有界异步写入；Response Store 按 trace 幂等保存隐私受限投影，冲突或写失败 fail-closed。诊断 CLI 支持 trace/response/feedback/稳定身份只读查询，retention 默认 dry-run。
- 8790/8795 看门狗精确核对端口、PID、Python 和完整参数；使用单实例锁、活 PID 保护与候选启动验证，禁止按端口杀未知进程。
- 阶段 3.1 与 3.2 已完成：冻结 `TaskStateSnapshotV1`，实现纯构造器、A3→A2 双锁父子各单读、standalone A2 单锁单读、frozen-state 零重读及异常/矩阵 fail-closed；完成时 51 项 task-state、全仓 1035 项通过。
- 3.3.1 已冻结 exact typed V1 公共映射；3.3.2 已接入 `GET /api/session`，成功载荷保留旧字段并新增根级 `task_state`，不可读状态不二次读取，HTTP error 暂不带状态。
- 3.3.3 已接入 `POST /api/message`、`POST /api/image`、启用 A3 时的 `POST /api/a3/select` 及 `POST /api/reset`。正常、stale、取消及 HTTP 200 业务状态都使用响应时冻结快照；空任务取消和 reset 返回 canonical empty，活跃任务取消在清库前冻结 `CANCELLED`。JSON 缺 legacy/projection/exact typed V1 即 fail-closed；媒体重开要求完整 guard，并原子校验新的 legacy+typed 组合。

## In Progress

- 阶段 3.3 出口一致性进行中：3.3.1～3.3.3 DONE 且尚未部署；3.3.4 受控 HTTP error 需单独授权。
- 本地内测发布已完成；待账户侧配置 Cloudflare Access 与边缘登录限速后，再向 2～3 名测试者发放邀请码并观察 24～48 小时。

## Not Implemented

- Cloudflare Access、边缘登录限速和测试者邮箱名单仍需账户侧配置；应用内限速不能替代边缘策略。
- 8795 尚无 Trace/Response 诊断 UI；未来若需要，只能作为可选只读消费者。
- 3.3.4～3.3.6 的受控 HTTP error、stream result/error 和跨出口验收，以及 3.4 前端消费、3.5 启用门与阶段 4～6 尚未实现。
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
- 主费用库有 10 条早期 `glm-5v-turbo` 成功调用缺价格；原库保留 0 元，8795 以当前价目表重估，均不等同供应商实扣。
- Qwen 冷调用有长尾；1/2/55 队列保护额度，但第 4 个同时任务会直接繁忙，排队超过 55 秒需重试。
- 旧 `parse_chapter` 会把“第4章”映射为内部 `4力法`；严格入口对纯数字返回 `uncertain`，其他未迁移入口仍可能误搜。
- 邀请码转发会共享额度；签名 Cookie 不能阻止持码人主动共享，完成后落账也可能让最后一个在途任务略超阈值。
- Trace 写入为 fail-open，只以健康计数暴露丢失；WAL 双副本和只读检查降低风险，但不是绝对线性化快照。

## Do Not Do

- 不读取、提交或展示 API key、Tunnel token、邀请码明文、管理员密码、私有发放清单或本地敏感配置。
- 不把管理员认证并入用户会话，不把 8795 部署进 8790，也不让 8795 成为 Trace/Response 所有者。
- 不因后台 ID/哈希一致就假定旧邀请码可用；灾备还必须核对状态、登录和动态撤销。
- 不把邀请码身份改回会话 Cookie，不删除全站保险上限。
- 不跨章节搜索，不绕过项目脚本识别、过滤和排序；未授权时不把图片发给外部模型。
- 不把公共输出改造扩展到个人飞书入口，不随意停止 8788；部署只操作精确验证过的 8790/8795 进程。
- 不按端口批量杀进程，不覆盖活 PID 文件；身份核对失败时停在现场。
- 不在目标回复缺失时保存整段反馈历史，也不把反馈专用框选图重复注入普通聊天消息。

## Next Best Step

1. 等待用户单独授权后再开始 3.3.4 受控 HTTP error；不提前做 stream、3.4 或部署。
2. 账户侧为 8790/8795 配置独立 Cloudflare Access 与窄范围边缘限速后，再受控发放 3 个邀请码。
3. 观察 24～48 小时，以成功率、排队、裁图、候选质量、反馈错绑和估算费用决定后续；异常时先停用邀请码再撤销 Access。

## Important Commands

- `python -m unittest discover -s tests -p 'test_*.py'`
- `python -m unittest discover -v -s tests -p 'test_task_state_*.py'`
- `powershell -ExecutionPolicy Bypass -File scripts/tiku_admin_watchdog_8795.ps1`
- `powershell -ExecutionPolicy Bypass -File scripts/tiku_agent_watchdog_8790.ps1`
- `python scripts/run_tiku_agent_8790.py --help`
- `python scripts/tiku_diagnostics.py --help`
- `python scripts/tiku_retention.py --help`
- `python scripts/search_by_loads.py --help`
- `python search.py --help`
