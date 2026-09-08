# Project Memory

## Current State

- 截至 2026-09-08，阶段 4 及优化 4.5.1～4.5.5 完成，已合入本地主线 `codex/mainline-bounded-autonomy-v1` 并上线 8790，未推送。生产代码基线 `066c967`，后续文档提交不代表生产代码变更。
- 8790 固定 release 为 `.worktrees/8790-release-066c967`，计划任务 `Tiku Agent Web 8790` 显式启用 A2/A3 Checkpoint；runtime 沿用 `.tmp_tiku_agent_v2_prod_8790`，控制库沿用 8795，原会话未搬迁。源码采集开关默认仍关闭。
- A3-V1 生产链为整页理解 → 框选/裁图 → 双门禁 → 单题下行或多题选择 → A2/人工裁剪；8790、8896、8788 均已停四方向 RapidOCR，RapidOrientation 未接入。
- 8790 保留统一计费、反馈、动态额度及 1 运行/2 排队/55 秒队列；Trace/Response 独立于可替换的 8795。此次发布未改变 8788、8795、8896、8902 的服务进程。
- 全量 1466 项通过；用户五个请求的 33/33 Checkpoint 成功，Trace/unit 归属一致，12 个原图/裁图 Artifact 可读；核验时无失败、丢弃、积压或 Trace 丢失。范围与发布回退材料见 [Roadmap](roadmap.md#阶段-4-主线发布与生产验收2026-09-08)。

## Implemented

- A3-V1 以 `unit_id` 绑定题目、框和裁图，多题校验上限 10；服务端校验状态、编号和 unit 集合，媒体按期过期。
- 共享复筛为综合分 `>=90%` 全部，否则 Top 3；8896 为 `>=95%`。章节解析失败后仅经授权搜字母库。
- 使用量按父 workflow 页数和进入 A2 的题数统计；费用归属稳定邀请码，A3/子 A2 共用账本。反馈 v8 绑定服务端 Response，v7 只读兼容。
- 公共五态协议只输出注册 code/白名单字段；候选原子交付，媒体失败可重发，同题重试沿用 `search_id`。
- 8790 已实现有界队列、请求限长、登录限速和 JSON 工作线程；8795 管理登录同样限长限速，费用库异常时邀请码删除 fail-closed。
- Trace/Response Store 使用白名单、唯一终态和幂等隐私投影；诊断 CLI 只读，retention 默认 dry-run。
- 8790/8795 看门狗核对端口、PID、Python 和 argv；8790/8896 另绑定固定 checkout/入口，身份不明时 fail-closed。8790 启动复验 manifest、完整提交、干净 worktree 和 runtime；Limited 任务显式使用 UTF-8 与 `core.quotePath=false`。
- 阶段 3 已冻结 `TaskStateSnapshotV1`，完成纯构造、锁内单读、异常 fail-closed、跨出口一致快照、前端 branded 动作授权及固定 release；动作绑定 identity/revision/target，拒绝 stale/ABA，未知结果不自动重放，refresh-recovery 仅经 `/api/session` 对账并保留 pending fence/限定历史态补偿。
- 阶段 4 已贯通 A2/A3 九阶段、父子 revision、输入指纹、原图/裁图 Artifact、识别/章节/荷载/尺寸、筛选计数、候选排名分数和答案引用；图片、unit 与 Trace 精确绑定，不替代 Task State 或授权动作。
- 独立 Checkpoint/Artifact Store 具备可信 TTL、七项容量门、有限审计、孤儿清理和带备份的周期 retention；结构化记录 30 天，普通/失败图片 3/7 天，反馈/调查最多 365/90 天，无永久 hold。证据写失败不阻断业务，读取与管理仍 fail-closed。
- 候选/答案使用 `bank_id/chapter/relative_key/lookup_mode=current`；答案采集不复制题库字节，业务答案交付仍按原流程复制。原图/裁图保留 Artifact，裁图文件名不可变，schema 1 Artifact 与 schema 2 引用兼容。
- 冻结阶段输入后进入有界异步单消费者队列（含在途最多 128 项、8 MiB、120 秒）；只有实际提交才写 Trace 关联。已实现资源租约、等待预算、熔断、健康计数和有限停机；维护占锁时 Trace 在有界队列保留事件并可取消等待，不重试不确定的数据库提交。
- 主线保留受保护的后台 Trace 诊断和运营概览源码；本次未重启 8795，不能据主线合并推断后台运行版本。

## In Progress

- 8790 继续试用；本轮覆盖单题检索/答案、多题上传/选题/答案及登录/动态停用，继续观察错绑、裁图边界和证据健康，浏览器恢复场景仍需按样本核验。
- 方向评估保留既有离线基线，尚无可上线的误旋安全阈值；新样本及工作区文件状态须在恢复该任务时重新核验。

## Not Implemented

- Cloudflare Access、边缘登录限速和测试者邮箱名单仍需账户侧配置；应用内限速不能替代边缘策略。
- 阶段 5 幂等执行/父子控制、阶段 6 后台任务与 HTTP 流解耦尚未实现；阶段 7 暂停/继续仍延期。本轮完成阶段 4，不自动进入后续阶段。
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
- 真实烟测样本仍少；需关注错绑、跨题费用、客户端时间、多题混排、裁剪边界、小荷载、低清和旋转。
- 方向阈值未校准；现有阈值无法同时保证误旋安全和召回，不能直接上线。
- Qwen 冷调用有长尾；1/2/55 队列下第 4 个同时任务直接繁忙，等待超 55 秒需重试。
- 旧 `parse_chapter` 会把“第4章”映射为 `4力法`；严格入口对纯数字返回 `uncertain`，未迁移入口仍可能误搜。
- 邀请码转发会共享额度，完成后落账可能使最后一个在途任务略超阈值。
- 题库引用读取当前文件，不保存题库历史字节；移动、覆盖或删除原文件会改变可读证据，缺失时不找另一版补位。Checkpoint 过期/删除不会删除题库原文件。
- 超过队列容量会拒绝证据；样本无丢失不代表任意负载零丢失，请求总耗时也不是采集增量。性能边界见 [优化验收](../docs/checkpoint_optimization_phase4_5.md)。
- 合并诊断预览最多 100 行，长多题 Trace 可能被截断；需缩小范围核对完整关联，不能把预览截断判断为入库丢失。
- 当前保存尺寸筛选计数及复筛候选排名/分数，不保存每个候选的尺寸淘汰明细或复筛解释全文。
- 无 Web Lock 时任务入口 fail-closed，只允许会话对账；8896 浏览器完整路径已通过，发放前仍需覆盖测试者浏览器。
- NATAPP 静态资源和健康可达不证明公网登录恢复闭环。
- 8790 冷启动须等 PID 链稳定并跨 watchdog 周期复核，回退只停已捕获 PID。

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
2. 阶段 4 已完成；以真实问题决定下一项工作，经用户明确选择后再规划阶段 5/6，不提前实现暂停/继续。
3. 账户侧配置 8790/8795 的 Cloudflare Access 与边缘限速后，再受控发放邀请码。

## Important Commands

- `python -m unittest discover -s tests -p 'test_*.py'`
- `python -B -m unittest -q tests.test_checkpoint_optimization tests.test_checkpoint_async tests.test_checkpoint_resilience`
- `python scripts/run_tiku_agent_8790.py --help`
- `python scripts/tiku_diagnostics.py --help`
- `python scripts/tiku_checkpoint_diagnostics.py --help`
- `python scripts/tiku_retention.py --help`
- `python scripts/search_by_loads.py --help`
- `python search.py --help`
