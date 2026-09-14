# Project Memory

## Current State

- 交接日期：2026-09-12。当前工作目录为 `F:\cc\_worktrees\7-题库检索\phase6-background-execution`，分支 `codex/phase6-background-execution`。新对话必须在此 worktree 继续；主目录 `F:\cc\7-题库检索` 不是阶段 6 开发 checkout。
- 阶段 1～5 已完成；本 worktree 的 **6.1～6.3 DONE，6.4～6.6 未实现**。下一项是 6.4 网页刷新/断网恢复；阶段 7 暂停/继续仍延期。
- 已提交开发基线：6.1 `8dbfada`、6.2 `05d16e9`、6.3 `c14d71f`。交接前代码工作区干净，无开发半成品或运行中的开发任务；本次仅维护交接文档。
- 6.3 最终全量 **1,695 tests / OK，161.744 秒，无跳过**，含新增 26 项；日志 `.tmp_phase6_3/full-regression-final.log`。仅使用合成 provider 与隔离数据库，没有新增付费模型样本。
- 离线 HTTP 探针断流/重复查询后仍为 1 次调用、1 个 attempt、1 条 Response，数据库检查与 drain 通过。证据与限制见 [6.3 交付文档](../docs/phase6_3_http_protocol.md)。
- 阶段 6 仅本地开发，未推送、未合入主线、未上线。已有生产发布授权不延伸到本批；后续发布需单独授权。
- 本会话记录的 8790 最新发布是 `c5ec463`（Trace 分段耗时诊断），不是阶段 6；本次交接不重新探测生产进程。主线 `codex/mainline-bounded-autonomy-v1` 与当前 worktree 必须区分。

## Implemented

- 6.1 冻结接收/执行/观察边界、现有取消链和 F01～F18 验收目标；[完整计划](../docs/phase6_execution_plan.md) 是阶段 6 规范入口。
- 6.2 提供私有持久输入、原子接收、同键去重与独立 worker，复用原 execution 权威；A2 与 A3 五类操作已覆盖。保持 1 运行/2 排队/55 秒原截止时间，授权/额度在接收、领取、模型发送前重验；UNKNOWN 不盲目重发。见 [内核文档](../docs/phase6_2_background_kernel.md)。
- 6.3 提供 `/api/jobs` 提交、原键发现、只读查询/NDJSON 订阅，观察取消不取消业务；独立登录 Cookie/grant 支持注销撤销。`background_execution` 默认关闭，生产 launcher 尚未接入；旧业务接口在新模式明确拒绝。
- 6.3 冻结公共结果/媒体与 Response 归属；后台有界修复交付不重搜，重复观察/评分复用同一回复。历史快照不授权当前动作，HTTP/后台 Trace 分开。实现入口为 `background_http.py`、`background_publication.py`、`background_auth.py`、`background_trace.py`。
- 既有会话 TTL 由 `conversation_ttl.py` 单一定义、服务端裁决；未确认错误不清历史，本地到期但服务端存活时最多 60 秒后复查。保留旧阶段的恢复与失效提示行为。
- 阶段 3 TaskState 使用锁内单读、公开白名单和 branded 动作；跨题/跨 revision 拒绝，无 Web Lock 时关闭任务入口。前端仍在 `tiku_agent/demo_web/` 使用旧业务协议，尚未消费后台任务。
- A3-V1 以 unit_id 绑定整页理解、裁图、双门禁与 A2；多题上限 10。共享复筛 >=90% 全部，否则 Top 3；8896 为 >=95%，章节不明须授权后搜字母库。
- 费用归属稳定邀请码，父 A3/子 A2 共账本；阶段 5 持久幂等操作、租约、模型效果、费用 outbox、父子/逐题收据与配置版本，待对账阻止新调用。
- Trace/Response 使用受限投影与唯一终态，反馈 v8 绑定服务端 Response；新增 Trace 分段耗时只用于定位，未改变 0.5 秒写预算与既有重试语义。
- Checkpoint/Artifact 九阶段证据具备 TTL、容量、审计和异步有界采集；题库答案保留当前位置引用，原图/裁图保存 Artifact。证据不替代业务状态或动作授权。
- 8790/8795 的控制库认证、动态额度、登录限速、队列及固定 release/manifest 看门狗边界沿用；新阶段未修改生产或飞书入口。

## In Progress

- 当前开发停在 6.3 完成处，等待新对话继续 **6.4 网页恢复**；本次交接不提前实现该步骤。

## Not Implemented

- 6.4 网页保存操作键/凭据、重连恢复、结果去重和多标签页真实浏览器验收。
- 6.5 完整崩溃/重启、UNKNOWN、本地安全恢复、过期/撤销、文件清理竞争与版本切换矩阵；6.6 全门验收、真实样本范围及可回退发布。
- 阶段 7 暂停/继续；Cloudflare Access、边缘限速及测试者名单仍未完成账户侧核验。
- 可复用 8790 release 发布器尚无；RapidOrientation 校准/封装及 Paddle V2 继续延期。

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

- 6.3 的接口/ASGI 与子进程证据不替代 6.4 真实浏览器验收，也不等于 F01～F18 全门完成；旧网页直接接新模式会收到 409，不能先切生产开关。
- 后台结果区分业务 SUCCEEDED 与 publication READY/FAILED/UNAVAILABLE；旧快照只供展示，当前动作必须重新授权。查询不续会话，不得靠重连恢复已失效 epoch。
- 发布最多 3 次；容量/到期失败保留业务收据，不通过重新调用模型补旧结果。强制停机、未 drain 调用和版本迁移仍需 6.5 闭环。
- Trace marker 与诊断入库间崩溃可能丢事件；诊断不是业务权威。此前 10 次 Trace 写失败未补回，根因仍待分段证据；诊断为进程内有界数据，重启清空不等于修复。
- 真实模型样本仍偏少；后续关注父子错绑、跨题费用、多题混排、裁剪边界、小荷载、低清与旋转。供应商恰好一次不在保证范围内。
- 生产固定 release 是运行依赖，既有计划任务配置不证明无人登录冷启动；不能因阶段 6 本地完成就覆盖生产 checkout。
- Qwen 冷调用有长尾，容量外拒绝；邀请码共享额度、完成后记账可能使最后一个在途略超阈值。NATAPP 可达不等于公网鉴权闭环。
- 旧 parse_chapter 把“第4章”映射为 `4力法`，严格入口对纯数字返回 uncertain；历史题库引用读取当前文件，移除文件会影响证据可读性。
- 阶段 5 产生新操作后不能回退旧会话库；必须保留执行库、费用和媒体，以兼容状态的修复版本处理。

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

1. 在本 worktree 先读 `docs/phase6_execution_plan.md`、`docs/phase6_3_http_protocol.md`，再定位 `tiku_agent/demo_web/demo.js` 的请求协调/本地存储/提交/恢复逻辑及 `execution_control.js`、`task_state.js`；从 6.4 的操作键持久化、原键发现和只读重连切片开始，不重做后台内核。
2. 延续 Web Lock、pending fence、服务端 TTL 与 epoch/revision 校验；补齐刷新、断网、多标签页、ACK 丢失及稳定 Response 去重的隔离浏览器验证。只查询原任务，不自动重发业务。
3. 6.4 完成后再进入 6.5/6.6。测试/原型继续用本 worktree 独立 runtime、Cookie、媒体与控制测试配置；真实付费采样、推送和上线按各自授权处理。

## Important Commands

- `Set-Location -LiteralPath 'F:\cc\_worktrees\7-题库检索\phase6-background-execution'`
- `git status --short`；`git log -3 --oneline`
- `Get-Content -LiteralPath 'docs/phase6_execution_plan.md'`
- `python -B scripts/run_phase6_http_probe.py`（仅创建新隔离 runtime，不监听端口）
- `python -B -m unittest discover -s tests -q`（全量回归；改动前无需为交接重复运行）
