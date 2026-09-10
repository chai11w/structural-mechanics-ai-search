# Project Memory

## Current State

- 截至 2026-09-10，阶段 1～5 已完成；阶段五已从 `codex/phase5-idempotent-execution` 快进合入本地主线 `codex/mainline-bounded-autonomy-v1`，未推送。阶段 6/7 未启动。
- 8790 任务 `Tiku Agent Web 8790` 已启用 durable execution 和 A2/A3 Checkpoint。runtime `.tmp_tiku_agent_v2_prod_8790`、8795 控制库沿用；1 父/1 等待选题子会话迁至 `execution.sqlite3`，旧库及媒体保留。固定版本、备份见 [发布记录](../docs/phase5_8790_release.md)，主线更新不改变生产版本。
- A3-V1 经过整页理解、裁图、双门禁与选题进入 A2；8790/8896/8788 已停四方向 RapidOCR，RapidOrientation 未接入。
- 上线前全仓 1623 项和两组真实隔离浏览器验收通过；生产只读冒烟、认证、旧库哈希及看门狗稳定检查通过。合入主线后追加 65 项回归通过，业务代码与发布版本一致。未额外调用真实模型，旧阶段样本不作新验收；见 [验收矩阵](../docs/phase5_acceptance_matrix.md)。

## Implemented

- A3-V1 以 `unit_id` 绑定题目、框和裁图，多题校验上限 10；服务端校验状态、编号和 unit 集合，媒体按期过期。
- 共享复筛为综合分 `>=90%` 全部，否则 Top 3；8896 为 `>=95%`。章节解析失败后仅经授权搜字母库。
- 使用量按父 workflow 页数和进入 A2 的题数统计；费用归属稳定邀请码，A3/子 A2 共用账本。反馈 v8 绑定服务端 Response，v7 只读兼容。
- 公共五态协议只输出注册 code/白名单字段；候选原子交付，媒体失败可重发，同题重试沿用 `search_id`。
- 8790 已实现有界队列、请求限长、登录限速和 JSON 工作线程；8795 管理登录同样限长限速，费用库异常时邀请码删除 fail-closed。
- Trace/Response Store 使用白名单、唯一终态和幂等隐私投影；诊断 CLI 只读，retention 默认 dry-run。
- 看门狗核对端口、PID、Python 和 argv；8790/8896 绑定固定 checkout/入口。8790 复验 manifest、完整提交、干净 worktree 和 runtime；Limited 任务使用 UTF-8 与 `core.quotePath=false`。
- 阶段 3 的 `TaskStateSnapshotV1` 支持锁内单读、异常 fail-closed、跨出口一致快照和 branded 动作授权；绑定 identity/revision/target，拒绝 stale/ABA。刷新按会话对账保留 pending fence，未知结果不自动重放。
- 阶段 4 贯通 A2/A3 九阶段及父子 revision、输入、Artifact、识别与筛选、候选分数和答案引用；图片/unit/Trace 绑定，不替代 Task State 或动作授权。
- Checkpoint/Artifact 支持 TTL、七项容量、审计、孤儿清理和备份 retention：记录 30 天，普通/失败图片 3/7 天，反馈/调查上限 365/90 天，无永久 hold。写失败不阻断业务，读/管理 fail-closed。
- 候选/答案使用 `bank_id/chapter/relative_key/lookup_mode=current`，只采引用，业务仍复制答案。原图/裁图保留 Artifact，裁图名不可变；schema 1 Artifact 兼容 schema 2 引用。
- Checkpoint 异步单消费者含在途最多 128 项、8 MiB、120 秒，提交后才关联 Trace；支持租约、熔断、健康和有限停机。维护占锁时 Trace 有界等待且可取消，不重试不确定提交。
- 主线含后台 Trace 诊断和运营概览；8795 未重启，运行版本须另核验。
- 阶段五持久化幂等操作、租约、模型效果、费用 outbox、父子收据及逐题结果；绑定会话代次、任务和配置版本。登记/抢占/出队/发送前核对费用，待对账阻止新调用，未知效果不自动重放。
- 前端隐藏诊断面板仍保留可见重置核对入口，刷新复用原命令。非空旧库须显式迁移；已有执行库时禁用开关会拒绝旧写入路径。

## In Progress

- 阶段五上线后继续收集真实样本，核对正常单题/多题、重试恢复、费用归属和证据健康；本次不启动收费采样或后续阶段。
- 方向评估保留既有离线基线，尚无可上线的误旋安全阈值；新样本及工作区文件状态须在恢复该任务时重新核验。

## Not Implemented

- Cloudflare Access、边缘登录限速和测试者邮箱名单仍需账户侧配置；应用内限速不能替代边缘策略。
- 阶段 6 后台任务与 HTTP 流解耦未实现；阶段 7 暂停/继续仍延期，须用户另行选择后规划。
- 尚无可复用的 8790 计划任务 release 发布器；`switch_tiku_agent_8790_control.ps1` 仅迁移控制库，不负责任务切换或代码回退。
- RapidOrientation 封装、阈值、8896 影子和 8790 发布未实现；需提取 ONNX 置信度并固定版本/模型哈希。
- Paddle splitter 驱动的裁剪及回退属于 A3 V2，暂不继续。

## Architecture Rules

- 8795 与 8790 分离；Trace/Response Store 和诊断查询独立于 8795，后者不是数据所有者。
- 管理认证、Cookie、运行目录和控制数据不得与用户会话混用；8790 只读邀请码哈希，8795 加密保存新建/重置码。
- 控制库与 AES-GCM 密钥必须成对迁移和备份；迁移前核对 ID、哈希、状态和认证版本，冲突禁止写入。
- 费用归属稳定邀请码，不按临时 Cookie；预算准入前检查、完成后落账，保留单码额度和全站上限。
- 工具内部诊断与公共输出分层；新 Agent HTTP/Web 只接受注册错误码和白名单字段，个人飞书入口不纳入该边界。
- A3 裁剪固定为 GLM bbox + Pillow；若恢复方向预处理，优先独立评估 ONNX RapidOrientation，不恢复 Paddle 主链或默认四方向 OCR。
- live 题库根为 `D:\桌面\答疑、帮做\结构力学\帮做`，字母库为相邻 `帮做_字母库`；仓库 Excel 是历史副本。
- 题库写操作必须 plan → confirm → backup → execute；服务端口、Cookie、状态、媒体和日志保持隔离。

## Known Risks

- Cloudflare Access 和边缘登录限速尚未从账户侧核验；完成前不应把公网地址和邀请码同时发给测试者。
- 阶段五真实模型样本尚未补采；关注错绑、跨题费用、客户端时间、多题混排、裁剪边界、小荷载、低清和旋转。
- 方向阈值未校准；现有阈值无法同时保证误旋安全和召回，不能直接上线。
- Qwen 冷调用有长尾；1/2/55 队列下第 4 个同时任务直接繁忙，等待超 55 秒需重试。
- 旧 `parse_chapter` 会把“第4章”映射为 `4力法`；严格入口对纯数字返回 `uncertain`，未迁移入口仍可能误搜。
- 邀请码转发会共享额度，完成后落账可能使最后一个在途任务略超阈值。
- 题库引用读取当前文件，不保存题库历史字节；移动、覆盖或删除原文件会改变可读证据，缺失时不找另一版补位。Checkpoint 过期/删除不会删除题库原文件。
- 超过队列容量会拒绝证据；样本无丢失不代表任意负载零丢失，请求总耗时不是采集增量。上线前曾有 3 次 Trace 写入超时，重启计数归零不代表根因已修复。见 [优化验收](../docs/checkpoint_optimization_phase4_5.md)。
- 诊断预览最多 100 行，长 Trace 需缩小范围，截断不等于丢失；尺寸只存筛选计数，复筛只存排名/分数，不含逐候选淘汰明细或完整解释。
- 无 Web Lock 时任务入口 fail-closed，只允许会话对账；8896 浏览器完整路径已通过，发放前仍需覆盖测试者浏览器。
- NATAPP 静态资源和健康可达不证明公网登录恢复闭环。
- 8790 冷启动须等 PID 链稳定并跨 watchdog 周期复核，回退只停已捕获 PID。
- 阶段五产生新业务操作后，不能直接回退旧会话库；须保留执行库、费用证据和媒体，以兼容状态的修复版本处理。供应商恰好一次和未知调用自动恢复不在保证范围内。

## Do Not Do

- 不读取、提交或展示 API key、Tunnel token、邀请码明文、管理员密码、私有发放清单或本地敏感配置。
- 不把管理员认证并入用户会话，不把 8795 部署进 8790，也不让 8795 成为 Trace/Response 所有者。
- 不因后台 ID/哈希一致就假定旧邀请码可用；灾备还必须核对状态、登录和动态撤销。
- 不把邀请码身份改回会话 Cookie，不删除全站保险上限。
- 不跨章节搜索，不绕过项目脚本识别、过滤和排序；未授权时不把图片发给外部模型。
- 无新证据时不重新默认开启四方向 OCR，也不把 RapidOrientation 当作已验证替代。
- 不把公共输出改造扩展到个人飞书入口，不随意停止 8788；目标回复缺失时不保存整段反馈历史。
- 不按端口批量杀进程，不覆盖活 PID 文件；身份核对失败时停在现场。
- 8790 发布须固定 release/manifest、备份数据与任务 XML 并按完整身份切换；控制库迁移脚本不得充当发布器或影响 8788/8794/8795。
- 不读或操作 8888；它与 8790 无关。未经用户明确授权不改或重启 NATAPP。

## Next Best Step

1. 继续在 8790 收集真实失败样本，按 Trace → Checkpoint → 原图/裁图/题库引用定位；关注健康中的失败、丢弃、积压与维护等待，发现缺口再做有范围的修复。
2. 阶段五已完成并合入主线；以生产问题决定修复优先级，经用户选择后再规划阶段 6，不提前实现暂停/继续。
3. 账户侧配置 8790/8795 的 Cloudflare Access 与边缘限速后，再受控发放邀请码。

## Important Commands

- `python -m unittest discover -s tests -p 'test_*.py'`
- `python scripts/run_tiku_agent_8790.py --help`
- `python scripts/tiku_diagnostics.py --help`
- `python scripts/tiku_checkpoint_diagnostics.py --help`
- `python scripts/tiku_retention.py --help`
- `python scripts/search_by_loads.py --help`
- `python search.py --help`
