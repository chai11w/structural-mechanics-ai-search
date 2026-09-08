# Project Memory

## Current State

- A3-V1 已在 `8790` 生产：整页理解 → 框选/裁图 → 双门禁 → 单题下行或多题选择 → A2/人工裁剪；8790、8896、8788 均已停四方向 RapidOCR，RapidOrientation 未接入。
- 端口隔离保持：8788 飞书、8790 生产、8795 后台；8896 已修复候选答案误拒绝和拖图退回首页，全仓 1236 项及浏览器连续两题/刷新恢复通过。
- 8790 读取 8795 控制库；A1/A2/A3 与子 A2 统一计费，队列为 1 运行/2 排队/55 秒，支持 FIFO、流关闭撤队和同锁去重。Trace/Response 独立于可替换的 8795；8 个 live SQLite 库及反馈、费用、停用闭环已验收。
- 8790 由 `answer-session-v9` 固定 release 运行；身份链、健康/Trace、静态资源、认证及含任务 XML/Git bundle/9 库的回退均已验收。
- 已创建 3 个独立、7 天、每日 3 元模型估算额度的内测邀请码并验证；明文只经 8795 受控复制，不进入日志或项目文件。
- 阶段 4 IN_PROGRESS；4.1、4.2 已完成，4.3/4.4 尚未开始。4.2 不接入 A2/A3 自动 Checkpoint 采集，后续仍须单独受控开发和启用。

## Implemented

- A3-V1 以 `unit_id` 绑定题目、框和裁图，多题校验上限 10；服务端校验状态、编号和 unit 集合，媒体按期过期。
- 共享复筛为综合分 `>=90%` 全部，否则 Top 3；8896 为 `>=95%`。章节解析失败后仅经授权搜字母库。
- 使用量按父 workflow 页数和进入 A2 的题数统计；费用归属稳定邀请码，A3/子 A2 共用账本。反馈 v8 绑定服务端 Response，v7 只读兼容。
- 公共五态协议只输出注册 code/白名单字段；候选原子交付，媒体失败可重发，同题重试沿用 `search_id`。
- 8790 已实现有界队列、请求限长、登录限速和 JSON 工作线程；8795 管理登录同样限长限速，费用库异常时邀请码删除 fail-closed。
- Trace/Response Store 使用白名单、唯一终态和幂等隐私投影；诊断 CLI 只读，retention 默认 dry-run。
- 8790/8795 看门狗核对端口、PID、Python 和 argv；8790/8896 另绑定固定 checkout/入口，身份不明时 fail-closed。8790 启动复验 manifest、完整提交、干净 worktree 和 runtime；Limited 任务显式使用 UTF-8 与 `core.quotePath=false`。
- 阶段 3 已冻结 `TaskStateSnapshotV1`，完成纯构造、锁内单读、异常 fail-closed、跨出口一致快照、前端 branded 动作授权及固定 release；动作绑定 identity/revision/target，拒绝 stale/ABA，未知结果不自动重放，refresh-recovery 仅经 `/api/session` 对账并保留 pending fence/限定历史态补偿。
- 阶段 4.1 已冻结九阶段、父子 revision、输入指纹与 Artifact descriptor；结构化 Checkpoint 30 天，普通/失败图片 3/7 天，反馈/调查最多 365/90 天，无永久 hold；没有 Store/I/O、runtime 或生产采集，33 项契约及全仓 1274 项回归通过。
- 阶段 4.2 已完成独立 Checkpoint/Artifact Store、可信 TTL 与过期读取拒绝、七项写前容量门、有限审计与清理槽位、Artifact tombstone/孤儿清理、Checkpoint/Trace retention plan/apply、SQLite 在线备份与 manifest 校验、持久 Trace store identity、共享维护锁及 8790 周期 runner；定向 62 项及全仓回归通过。A2/A3 自动采集仍关闭。

## In Progress

- 8790 `answer-session-v9` 进入试用；新内置浏览器无生产 Cookie，需在登录浏览器完成连续两题与刷新恢复。
- 方向评估未实现：已提交 9×4 基线为 RapidOrientation/PP-LCNet `33/36`；主工作区另有未提交的 frontdoor 6×4 与 evaluator/manifest，`real_cases` 仍为空。OCR `20/24`、Rapid `17/24`，尚无安全阈值。
- 主工作区落后远端且脏；旧 `fence-login-v9` 会移除最终补偿和无 Cookie 测试，不得并入。`a3_routing_baseline/` 15 图重复；治理前禁止 pull/reset/删除。
- 远端主线在 8790 release 后新增可比单尺寸硬过滤及测试，尚未部署 8790，不能视作当前生产行为。
- 阶段 4.3/4.4 尚未开始；A2/A3 自动 Checkpoint 采集保持关闭，直到后续受控采集方案完成评审和验收。

## Not Implemented

- Cloudflare Access、边缘登录限速和测试者邮箱名单仍需账户侧配置；应用内限速不能替代边缘策略。
- 阶段 4.3～6 尚未实现；4.2 是 4.3/4.4 自动采集的强制前置门，当前已具备门禁能力但未启用采集。
- 尚无可复用的 8790 计划任务 release 发布器；`switch_tiku_agent_8790_control.ps1` 仅迁移控制库，不负责任务切换或代码回退。
- RapidOrientation 封装、阈值、8896 影子和 8790 发布未实现；需提取 ONNX 置信度并固定版本/模型哈希。
- Paddle splitter、全自动裁剪及自动/人工回退属于 A3 V2，暂不继续。

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
- 4.2 已提供 Trace/Checkpoint 周期 retention plan/apply 和共享维护锁；A2/A3 采集仍关闭，后续接入时须继续遵守证据写入 fail-open、读取与管理 fail-closed。
- 无 Web Lock 时任务入口 fail-closed，只允许会话对账；8896 浏览器完整路径已通过，发放前仍需覆盖测试者浏览器。
- NATAPP 静态资源和健康可达不证明公网登录恢复闭环。
- 8790 冷启动约 15～18 秒；须等 PID 链稳定并跨 watchdog 周期复核，回退只停已捕获 PID。

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

1. 在已有登录 Cookie 的浏览器实际使用 8790，完成连续两题、答案返回和刷新恢复；若再出现会话提示，保留页面、操作顺序和题图，先停止继续操作并对照 8896 定位。
2. 在独立 worktree 规划并评审 4.3 A2 受控采集；接入前沿用 4.2 的七项容量、TTL、retention 和健康降级门禁。
3. 账户侧配置 8790/8795 的 Cloudflare Access 与边缘限速后，再受控发放邀请码。

## Important Commands

- `python -m unittest discover -s tests -p 'test_*.py'`
- `python -B -m unittest -q tests.test_checkpoint_contract`
- `python scripts/run_tiku_agent_8790.py --help`
- `python scripts/tiku_diagnostics.py --help`
- `python scripts/tiku_retention.py --help`
- `python scripts/search_by_loads.py --help`
- `python search.py --help`
