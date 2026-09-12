# Project Memory

## Current State

- 截至 2026-09-12，阶段 1～5 完成，6/7 未启动；工作主线 `codex/mainline-bounded-autonomy-v1` 已推送 `e08efa2`。已清理合并完的 `codex/mainline-fix` 及工作区；较早分支保留，今日 `codex/lida-bank-release` 由另一任务处理。
- 8790 已发布 `e08efa2633f9b26e4a70047fe3bbb92ac43fd087`，固定工作区 `.worktrees/8790-release-e08efa2`；前版 `0be3808` 及旧回退目录保留。runtime、8795 控制库、durable execution 与 A2/A3 Checkpoint 沿用。8 库备份、任务 XML、验证证据见 `F:\cc\_backups\7-题库检索\2026-09-12\web-session-e08efa2\`。
- 本版 1629 项回归通过；四份线上前端资产匹配、未登录 API 返回 401，看门狗跨周期稳定。真实样本：22 次操作成功、75 个模型效果与 52 条费用已确认、15 个父子交接已提交、2 个多题批次；费用/父子归属一致。本轮无新增模型调用，不替代登录态浏览器人工验收。
- A3-V1 经整页理解、裁图、双门禁与选题进入 A2；8790/8896/8788 已停四方向 RapidOCR。
- 阶段 4、阶段 5 的验收范围仍按各自批次解释，本批只读冒烟不替代真实样本验收。

## Implemented

- 会话生命周期由服务端裁决；无匹配请求确认的注册错误仅显示、不清历史；本地到期而服务端仍存活时，最多 60 秒后再查，不延长活动时间。无服务端会话但本地仍新鲜时保留内容并标记“已失效”。
- 会话时长单一出处：`conversation_ttl.py` 供会话库、协调保留、执行库会话与 A3 裁图媒体共用，并经会话响应下发 `conversation_ttl_seconds`，浏览器倒计时与文案取自该值。
- 失败照实显示：客户端只把服务端注册表内的 (layer, code) 组合原样呈现（parity 测试锁定），未知或错配仍 fail-closed；Trace 写入把“抢锁失败”（30 秒内重试）与“中途被打断”（丢弃、不重放）分开，健康可区分 kind。
- A3-V1 以 `unit_id` 绑定题目、框和裁图，多题校验上限 10；服务端校验状态、编号与 unit 集合，媒体按期过期。
- 共享复筛为综合分 `>=90%` 全部，否则 Top 3；8896 为 `>=95%`；章节解析失败后仅经授权搜字母库。
- 使用量按父 workflow 页数与进入 A2 的题数统计；费用归属稳定邀请码，A3/子 A2 共用账本；反馈 v8 绑定服务端 Response，v7 只读兼容。
- 五态协议、Trace/Response 使用注册 code/白名单、唯一终态与幂等隐私投影；诊断 CLI 只读、retention 默认 dry-run。候选原子交付，媒体可重发，同题重试沿用 `search_id`。
- 8790 与 8795 分别实现有界队列、请求限长与登录限速，8790 另有 JSON 工作线程；费用库异常时邀请码删除 fail-closed。
- 看门狗核对端口、PID、Python 与 argv，并绑定固定 checkout/入口与 manifest；主线含后台 Trace 诊断与运营概览，8795 运行版本须另核验。
- 阶段 3 的 `TaskStateSnapshotV1` 支持锁内单读、异常 fail-closed、跨出口一致快照与 branded 动作授权；拒绝 stale/ABA，未知结果不自动重放。
- 阶段 4 贯通 A2/A3 九阶段及父子 revision、输入、Artifact、识别筛选、候选分数与答案引用；不替代 Task State 或动作授权。
- Checkpoint/Artifact 有 TTL、容量、审计、清理与 retention（记录 30 天、图片 3/7 天、反馈/调查 365/90 天）；异步采集上限 128 项、8 MiB、120 秒，写失败不阻断业务，读/管理 fail-closed。
- 候选/答案以 `bank_id/chapter/relative_key/lookup_mode=current` 只采引用，业务仍复制答案；原图/裁图保留 Artifact，裁图名不可变。
- 阶段五持久化幂等操作、租约、模型效果、费用 outbox、父子收据与逐题结果，绑定会话代次、任务与配置版本；待对账阻止新调用。
- 前端隐藏诊断面板但保留可见重置核对入口；非空旧库须显式迁移，已有执行库时禁用开关会拒绝旧写入路径。

## In Progress

- Trace 超预算丢事件仍需定位；本次已保存重启前健康基线并完成现有业务样本核查。后续使用继续关注过期/恢复、错误提示与证据健康，不自动推进收费采样或后续阶段。
- 方向评估仍为离线基线；恢复前须复核样本与工作区。

## Not Implemented

- Cloudflare Access、边缘登录限速和测试者邮箱名单仍需账户侧配置；应用内限速不能替代边缘策略。
- 阶段 6 后台任务与 HTTP 流解耦未实现；阶段 7 暂停/继续仍延期，须用户另行选择后规划。
- 尚无可复用的 8790 计划任务 release 发布器；本批两次发布均为受控手工流程，`switch_tiku_agent_8790_control.ps1` 只迁移控制库。
- RapidOrientation 封装、阈值、8896 影子和 8790 发布未实现；需提取 ONNX 置信度并固定版本/模型哈希。
- Paddle splitter 驱动的裁剪及回退属于 A3 V2，暂不继续。

## Architecture Rules

- 会话生命周期只有服务端一个权威：时长单一定义在 `tiku_agent/conversation_ttl.py`；客户端本地时钟只提出问题、不下判决，也不基于本地时间删除用户可见内容。
- 8795 与 8790 分离；Trace/Response Store 与诊断查询独立于 8795，后者不是数据所有者。
- 管理认证、Cookie、运行目录与控制数据不得与用户会话混用；8790 只读邀请码哈希，8795 加密保存新建/重置码。
- 控制库与 AES-GCM 密钥成对迁移和备份；迁移前核对 ID、哈希、状态与认证版本，冲突禁止写入。
- 费用归属稳定邀请码而非临时 Cookie；预算准入前检查、完成后落账，保留单码额度与全站上限。
- 工具内部诊断与公共输出分层；新 Agent HTTP/Web 只接受注册错误码与白名单字段，个人飞书入口除外。
- A3 裁剪固定为 GLM bbox + Pillow；恢复方向预处理时优先独立评估 ONNX RapidOrientation，不恢复 Paddle 主链或默认四方向 OCR。
- live 题库根为 `D:\桌面\答疑、帮做\结构力学\帮做`，字母库为相邻 `帮做_字母库`；仓库 Excel 是历史副本。
- 题库写操作必须 plan → confirm → backup → execute；服务端口、Cookie、状态、媒体与日志保持隔离。

## Known Risks

- 8790 固定 release 是运行依赖。计划任务已配置失败每分钟重启、最多 999 次、无执行时限；当前看门狗正常。仅核验配置与运行稳定性，未故意终止生产进程测试恢复；登录触发不等于开机无人登录自启。
- 重启前 Trace dropped/write_failures 均为 10，最后为 `EvidenceDeadlineExceeded`（09-12 17:01:57 +08:00）；Checkpoint 29/29 成功、无积压。Trace 写入预算 0.5 秒，聚合计数无法定位耗时或逐次归类；历史失败未修复，重启归零不是修复证据。
- 阶段五真实模型样本仍偏少；关注错绑、跨题费用、客户端时间、多题混排、裁剪边界、小荷载、低清与旋转。
- Cloudflare Access 与边缘登录限速未从账户侧核验，NATAPP 可达也不证明公网登录闭环；无 Web Lock 时任务入口 fail-closed，发放前不应同时给出公网地址与邀请码，并需覆盖测试者浏览器。
- 方向阈值未校准，无法同时保证误旋安全与召回，不能直接上线。
- Qwen 冷调用有长尾：1 运行/2 排队/55 秒等待，超额拒绝或超时重试；超容量可丢证据，样本无丢失不保证任意负载。
- 旧 `parse_chapter` 会把“第4章”映射为 `4力法`；严格入口对纯数字返回 `uncertain`，未迁移入口仍可能误搜。
- 邀请码转发会共享额度，完成后落账可能使最后一个在途任务略超阈值。
- 题库引用读取当前文件、不保存历史字节，移动或删除原文件会改变可读证据；诊断预览最多 100 行，截断不等于丢失，复筛只存排名与分数。
- 阶段五产生新业务操作后不能直接回退旧会话库；须保留执行库、费用证据与媒体，以兼容状态的修复版本处理，供应商恰好一次不在保证范围内。

## Do Not Do

- 不读取邀请码明文、私有发放清单或隧道凭据；密钥和配置边界见根 AGENTS.md。
- 不把管理员认证并入用户会话，不把 8795 部署进 8790，也不让 8795 成为 Trace/Response 所有者。
- 不因后台 ID/哈希一致就假定旧邀请码可用；灾备还必须核对状态、登录和动态撤销。
- 不把邀请码身份改回会话 Cookie，不删除全站保险上限。
- 不跨章节搜索，不绕过项目脚本识别、过滤和排序；未授权时不把图片发给外部模型。
- 无新证据时不重新默认开启四方向 OCR，也不把 RapidOrientation 当作已验证替代。
- 不把公共输出改造扩展到个人飞书入口，不随意停止 8788；目标回复缺失时不保存整段反馈历史。
- 不按端口批量杀进程，不覆盖活 PID 文件；身份核对失败时停在现场。
- 不删除或移动正在运行的 8790 release 目录；发布须固定 release/manifest、备份数据与任务 XML 并按完整身份切换。
- 不读或操作 8888；它与 8790 无关。未经用户明确授权不改或重启 NATAPP。

## Next Best Step

1. 对比本次备份与 `/health`，以备份库隔离定位 Trace 超预算耗时；不能靠放宽预算或重启宣告修复。
2. 后续使用观察会话过期/恢复与错误提示；本轮现有样本核查已完成，未重放用户任务。
3. 账户侧配置 8790/8795 的 Cloudflare Access 与边缘限速后再受控发放邀请码；阶段 6 需用户选择后规划，不提前实现暂停/继续。

## Important Commands

- `python -m unittest discover -s tests -p 'test_*.py'`
- `python scripts/run_tiku_agent_8790.py --help`
- `python scripts/tiku_diagnostics.py --help`
- `python scripts/tiku_checkpoint_diagnostics.py --help`
- `python scripts/tiku_retention.py --help`
