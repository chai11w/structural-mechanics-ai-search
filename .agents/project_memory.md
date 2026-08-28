# Project Memory

## Current State

- 8898 影子已用 RapidOCR 纯文字区域四方向回正替换错误的横图固定顺时针规则；只在分流确认 A3 后执行，A1/A2 和 8790/8896 不启用。9 张源图生成的 36 项方向回归为 36/36，OCR 版本与三份 ONNX 模型 SHA-256 已锁定，文字优势不足时保持原图。
- 测试集现按功能归档：`test_sets/routing/a1_a2_a3` 保存 15 张分流样本，`test_sets/orientation/a3_text_rotation` 复用 9 张 A3 源图生成 36 项方向样本；两套均有 SHA-256 manifest，旋转相关改动上线前必须全量通过方向套件。
- A3-V1 已在 `8790` 生产：Qwen 整页理解 → GLM 全题框选 → Pillow 本地裁图 → Qwen 并发完整性/外荷载门禁 → 单题下行或多题选择 → A2/人工裁剪；OpenCV/Paddle 不在主线启动链。
- 端口隔离保持：8788 飞书、8790 生产、8795 后台、8896 验收当前监听；8794 无监听，8891/8897 为分流/回退端口。
- 8790 读取 8795 控制库认证；A1/A2/A3 与子 A2 统一计费并按题汇总流程/反馈。小规模内测版已在 live 精确冷启动并由独立看门狗监管。
- 8790 有界队列启用：默认 1 个运行、2 个排队、55 秒等待；FIFO、防插队、流关闭撤队，同会话/同锁分片不重复占运行槽。
- 8795 仍是可替换的过渡后台；live 已厘清页/题、模型调用/请求次数、记录时间/页面等待/反馈时间及“模型估算费用”文案，本轮未增加 Trace 页面。
- Trace/Response 主链独立于 8795。发布前历史覆盖差异已审计；发布后真实烟测的 Response、反馈、费用、Trace、身份和时间顺序均能对应。
- 8 个 live SQLite 库在发布前后均通过 `quick_check`、`integrity_check`；烟测完成 A2、A3、追问、正/负反馈、费用展示和停用立即失效，4 条新反馈全部绑定服务端 Response。
- 全仓 1035 项回归通过；真实烟测发现并修复 JSON 长模型调用阻塞健康检查的问题，长请求期间 8 次健康探测均成功且 PID 未漂移。
- 8795 控制目录 ACL 已限制为当前服务账号、SYSTEM 和管理员；仓库外受限 Git Bundle、8 库在线副本、控制库/密钥成对备份及脱敏 postflight 已验证。
- 已创建 3 个独立、7 天、每日 3 元模型估算额度的正式内测邀请码，逐个登录验证成功；明文只通过 8795 受控复制，不进入日志或项目文件。

## Implemented

- A3-V1 以 `unit_id` 绑定题目/框/裁图，多题并发校验上限 10；单题自动下行 A2，异常回人工裁剪。服务端校验状态、编号空间和原样 unit 集合，防重复搜索/完成。
- 空分组按引用关系简化或拒绝；schema 错误只存 code/简因 30 天，题图/裁图/候选/答案在新对话或 2 小时后过期。
- 共享复筛为综合分 `>=90%` 全部，否则可靠 Top 3；8896/V1+Qwen 为 `>=95%`。章节使用七存储键和三态解析，失败后仅经用户授权才搜字母库。
- 今日使用按上传父 workflow 统计页数、按进入 A2 的题目统计检索题数；费用归属稳定邀请码，父 A3/子 A2 共用账本。费用为 Token 与版本化价目表估算，不等同供应商实扣。
- 反馈 schema v8 校验 `rated_response_id`、conversation、identity/session 与有效期；旧 v7 行只读兼容。A3 反馈使用目标 Response 的服务端投影，不接受客户端覆盖历史快照。
- 公共五态协议只输出注册 code/白名单字段；候选原子交付，答案区分 0/部分/全部，媒体失败可重发，同题重试沿用 `search_id`。
- 8790 有界队列、请求体限长、邀请码登录限速和安全客户端地址已实现；JSON 长模型调用转入工作线程，健康检查不再被阻塞。8795 管理登录同样限长、限速，邀请码永久删除在费用库损坏/缺表/不可读时 fail-closed。
- Trace Store 使用严格白名单、每 trace 唯一终态和有界异步写入；Response Store 生成隐私受限投影并按 trace 幂等，冲突/写失败 fail-closed。两者均不依赖 8795。
- 独立只读诊断 CLI 支持按 trace/response/feedback/稳定身份有界查询；retention 默认 dry-run，apply 需仓库外计划、备份、停机确认和漂移校验。
- 8790/8795 看门狗固定端口，精确核对 PID、Python、完整参数和监听者；单实例锁、旧 PID 活进程保护、候选启动验证，禁止按端口杀未知进程。
- 阶段 3.1 已冻结 `TaskStateSnapshotV1` 契约；3.2.1 无 I/O 纯构造器、3.2.2 锁内 runtime wrapper 与 3.2.3 异常/矩阵测试均已完成，阶段 3.2 整体 DONE。A3 固定按 A3→A2 锁序让父子 store 各读取一次，standalone A2 只获取 A2 锁，frozen-state 入口不重锁/不重读；缺失、不可读、稳定未知状态、组合读取失败和受控文件证据均 fail-closed。实际父 route/phase、九个 child phase × 三种载体、17 个一致性 code、旧 revision 与残留 `CANCELLED` 均有测试证据；51 项 task-state 定向测试及全仓 1035 项回归通过。HTTP、stream、前端和既有 `session_snapshot()` 尚未接入 V1。
- 3.3.1 已冻结 exact typed V1 公共映射和顶层 `task_state` 位置；subclass、任意 mapping 和客户端同名字段不可绕过契约，URL/路径/凭据/异常样文在 typed contract 与纯 builder 中共用 fail-closed 判定。58 项 task-state 定向回归及全仓 1042 项回归通过；HTTP、stream、前端和既有 `session_snapshot()` 仍未接入 V1。

## In Progress

- 本地内测发布已完成；待账户侧配置 Cloudflare Access 与边缘登录限速后，把 3 个邀请码分别发给 2～3 名测试者并观察 24～48 小时。

## Not Implemented

- Cloudflare Access、边缘登录限速和测试者邮箱名单仍需账户侧配置；应用内限速不能替代边缘策略。
- 8795 尚未提供 Trace/Response 诊断 UI；本轮明确不增加，未来若成为整体后台再以只读消费者接入。
- 阶段 3.3.2～3.3.6 的 `/api/session`、JSON success、受控 HTTP error、stream result/error 和跨出口验收，以及 3.4 前端消费、3.5 启用门与阶段 4～6 尚未实现。
- Paddle splitter、全自动裁剪及自动/人工回退属于 A3 V2，暂不继续。
- 8890 影子期未完成费用报表；影子结果不能直接提升到 8790。
- 桁架高度几何计算未实现；模型只抄录明确标注。视觉重排和候选二次位置复筛仍待真实样本。
- retention 未安装周期调度、未真实 apply；各运行日志仍仅有 `policy_missing` 报告。

## Architecture Rules

- 8795 与 8790 保持独立；Trace/Response Store 和诊断查询独立于 8795，后者未来也只能做可选只读 UI，不是数据所有者。
- 管理员认证、Cookie、运行目录和控制数据不得与用户会话混用；8790 只读邀请码哈希，8795 加密保存新建/重置码。
- 控制库与 AES-GCM 密钥必须成对迁移和备份；迁移前核对 ID/哈希/状态/认证版本，冲突禁止写入。
- 费用归属稳定邀请码，不按临时 Cookie；预算准入前检查、完成后落账，保留单码额度和全站上限。价格和 usage 只能形成模型估算。
- 搜题耗时按权威 revision 从有界网页时间线重建；反馈媒体复制到案例目录，不依赖短期会话路径。
- 工具内部诊断与公共输出分层；新 Agent HTTP/Web 只接受注册错误码和白名单字段，个人飞书入口不纳入该边界。
- 8790/8896 的 A3-V1 裁图固定为 GLM bbox + Pillow；OpenCV/Paddle 是遗留实验，不恢复为生产依赖。
- live 题库根为 `D:\桌面\答疑、帮做\结构力学\帮做`，字母库为相邻 `帮做_字母库`；仓库 Excel 是历史副本。
- 题库写操作必须 plan → confirm → backup → execute；各服务端口、Cookie、状态、媒体和日志保持隔离。

## Known Risks

- Cloudflare Access 和边缘登录限速尚未从账户侧核验；在完成前不应把公网地址和邀请码同时发给测试者。
- 真实烟测已证明新反馈绑定与费用关联，但样本仍少；观察期继续关注错绑、跨题费用归属和客户端时间异常。
- 主费用库有 10 条早期 `glm-5v-turbo` 成功调用当时缺价格；原库保留 0 元，8795 用当前价目表重估。任何展示仍不是供应商实扣对账。
- GLM/A3 真实样本仍少，需观察多题混排、裁剪边界、小支座/小荷载、低清和旋转；保留 8896 `--disable-auto-crop` 与 8897。
- Qwen 冷调用有长尾；1/2/55 队列能保护额度但会让第 4 个同时任务直接繁忙，排队超过 55 秒需重试。
- 旧 `parse_chapter` 会把“第4章”映射为内部 `4力法`；严格目录入口对纯数字返回 `uncertain`，其他未迁移入口仍可能误搜。
- 邀请码被转发会共享同一额度；签名 Cookie 防伪造但不能阻止持码人主动共享。完成后落账也可能让最后一个在途任务略超阈值。
- Trace 写入为 fail-open，只以健康计数暴露丢失；WAL 双副本与只读检查降低跨代风险，但不是绝对线性化快照。
- 2026-07-29 曾有旧 API 密钥出现在进程命令行；旧进程已终止，用户暂不轮换。

## Do Not Do

- 不读取、提交或展示 API key、Tunnel token、邀请码明文、管理员密码、私有发放清单或本地敏感配置。
- 不把管理员认证并入用户邀请码会话，不把 8795 部署进 8790 进程，也不让 8795 成为 Trace/Response 所有者。
- 不因后台 ID/哈希一致就假定旧邀请码可用；灾备还必须核对状态、登录和动态撤销。
- 不把邀请码身份改回会话 Cookie，不删除全站保险上限。
- 不跨章节搜索，不绕过项目脚本识别、过滤和排序；不在未授权时把图片发给外部模型。
- 不把公共输出改造扩展到个人飞书入口，不随意停止 8788；部署只操作精确验证过的 8790/8795 进程。
- 不按端口批量杀进程，不覆盖活 PID 文件；身份核对失败时停在现场。
- 不在目标回复缺失时保存整段反馈历史，也不把反馈专用框选图重复注入普通聊天消息。

## Next Best Step

1. 在 Cloudflare 账户侧为 8790/8795 配置独立 Access，并为两条登录路径启用窄范围边缘限速；完成后从 8795 复制 3 个邀请码分别发放。
2. 观察 24～48 小时，以成功率、排队、A3 裁图、候选质量、反馈错绑和估算费用决定下一步；异常时先停用邀请码再撤销测试者 Access。
3. 状态快照 3.3.1 公共映射契约已完成；下一个可独立回退小步是 3.3.2 `/api/session` 接入，未获得新授权前不开始。后续仍按 JSON success、受控 HTTP error、stream 终态、跨出口验收分步进行，不提前进入 3.4，也不启用 8790。

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
