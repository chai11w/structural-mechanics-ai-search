# 5.3 操作登记、执行互斥与隔离运行

状态：本地实现及隔离验证完成，未上线。工作分支 `codex/phase5-idempotent-execution`；5.2 基础提交 `2b8a40e`。本批不改变现有生产 launcher 的默认行为。

## 执行契约

启用 `attach_execution(runtime, authority)` 后，A2 与 A3 的当前状态、任务历史、操作和尝试统一进入同一 `execution.sqlite3`。旧 session 库不再读写；有存量状态必须先完成 [5.2 离线迁移](phase5_2_state_contract.md)。启用标记持久保存，重启或提前创建的另一个 store 实例都不能退回无执行 token 的写入。

操作作用域是服务端验证的身份摘要、会话摘要、epoch 和客户端 key。每次请求仍经过鉴权；同 epoch 会话绑定同一身份，另一身份不能命中原收据。直接 Python runtime 调用属于可信进程内接口，调用方负责身份认证。

`execution_operations` 保存操作类型、影响执行的输入摘要、预期总状态版本、当前父子任务/动作目标、源码生产版本及前一操作 ID。前一操作是会话顺序关联，不表示已经证明是某个失败操作的安全重试；旧收据清理后该历史 ID 可以不再可读。

图片摘要按字节计算，临时上传文件名不影响重发；裁剪框、章节/上下文、unit 顺序、候选 revision/generation/rank 参与摘要。trace/request ID、进度回调及界面能力不参与。相同 key 改内容会冲突；相同图片新 key 是新的明确操作，不会按内容合并。

`execution_attempts` 为每次获得执行资格的尝试生成随机 token。`execution_task_attempts` 以外键将父/子任务历史连到实际尝试，并保留该次尝试写入的首末状态版本；A3→A2 的内部下行沿用同一次尝试。`execution_cost_runs` 在创建费用 collector 的 run ID 时记录 operation/attempt 关联；这只是归属，不代表费用提交已被确认。

## 原子登记与状态

所有登记、争用、版本检查和写入采用 SQLite `BEGIN IMMEDIATE`。进程内锁仍保留，跨进程执行权由数据库保证；父子公开快照按 A3 锁 → A2 锁 → 同一个数据库事务读取，元数据与业务快照来自同一读取范围。

| 持久状态 | 重发行为 |
| --- | --- |
| REGISTERED | 未取得执行权；按当前身份、版本和现有额度/队列准入争用同一操作 |
| RUNNING | 返回 EXECUTION_BUSY；不会创建第二个执行者 |
| SUCCEEDED | 复用原收据及冻结结果，不调用 agent/模型/检索；这里只表示调用返回并保存收据，业务错误仍保留原公共 code |
| UNKNOWN | 返回 EXECUTION_UNKNOWN；不会因超时、租约过期或重启而自动接管 |
| FAILED | 未取得执行资格即发现状态已变化；旧操作不能执行 |

默认租约 300 秒，每约 100 秒续期；单次尝试最长 1800 秒，租约不得越过该截止时间。每次状态写入、费用 run 绑定和结果提交检查 token、epoch、租约及截止时间。失权后旧执行者不能保存结果，但本批不承诺立即中断已发出的外部调用。

只有明确的队列取消/繁忙/额度拒绝，且总状态版本未改变、没有关联费用 run，才允许原操作回到 REGISTERED，旧尝试保留 FAILED。其余异常保守记 UNKNOWN；确定性副作用恢复和人工对账流程属于 5.4。

## HTTP 与直接调用

新启动会话先读取 `GET /api/session`，响应新增独立的 `execution` 对象：`schema=1`、`epoch`、`state_version`、`expires_at`。V1 task_state 及原五态协议原义保持。

所有启用后的可变请求要求 `X-Tiku-Operation`，内容严格为：

```json
{"key":"客户端生成的唯一操作编号","epoch":"服务端给出的32位小写十六进制epoch","state_version":0}
```

key 实际格式为 8～128 个字母、数字或 `:_.-`；state_version 使用服务器返回的整数，不由客户端自行递增。上传、文本、选题、准备、裁剪的 JSON/stream 路径共用逻辑操作；reset 也有收据，重发旧 reset 不会清除随后创建的新任务。

浏览器只从既有权威投影验证通过的响应接受 execution 元数据。操作开始时冻结 key/epoch/version，同一请求重发保持该信封；新动作生成新 key。既有 Web Lock、v6 fence、鉴权及恢复准入仍保留。已登记操作可穿过已消费 fence 查询原收据，随后仍检查相同输入和执行状态。刷新恢复仍只读对账，不自动重放业务。

`GET /api/operation?key=...&epoch=...` 在现有登录及 session 下返回 operation_id、kind、status 和最近最多 20 次尝试的 ID/状态。它不返回 token、原始输入或结果，不启动业务。未知记录返回 404。

JSON 执行冲突/失权返回 409，容量或时钟异常返回 503；流式响应已建立后通过原 error 事件输出相同注册 code。业务错误仍使用既有五态协议，新增 code 不授权自动重放。

可信 Python 调用示例：

```python
from uuid import uuid4
from tiku_agent.execution_operations import OperationRequest
from tiku_agent.execution_runtime import attach_execution
from tiku_agent.execution_store import ExecutionStore

authority = ExecutionStore("isolated-runtime/execution.sqlite3")
attach_execution(runtime, authority)  # 新建或已经离线迁移的 runtime
ctx = authority.context(session_id)
request = OperationRequest(uuid4().hex, ctx["epoch"], ctx["state_version"])
response = runtime.handle_text(session_id, "力法", operation_request=request)
# 同一操作的传输重发必须保留 request；用户新意图重新读取 ctx 并生成新 key。
```

直接 handle_image、handle_preanalyzed_image、handle_prechecked_image、handle_text、select_unit、prepare_units、handle_crop、clear 均受同一执行门约束。只把 store 换成统一适配器、却不 attach 执行门的 runtime 会拒绝运行。

## 结果、媒体与容量

收据保存公共回复和冻结的 V1/legacy 视图、必要文件引用与哈希，不复制完整 AgentState、模型原文或原始 intent 分析。重发时文件缺失、内容变化或源码生产摘要变化会返回 EXECUTION_RESULT_UNAVAILABLE，不重新检索或生成替代答案。

生产摘要覆盖仓库根 Python、tiku_agent Python/Prompt、tiku_shared Python。它用于拒绝跨代码版本静默重放；不是模型供应商版本、动态配置或实时题库版本的完整指纹，不能用于跨操作阶段产物缓存。后者本批未实现。

A3 媒体交付失败后的重新开放动作使用原 key 派生的独立操作，绑定原 unit/revision/generation。重复交付失败复用这条修正收据，避免反复改状态。业务完成、文件保存、浏览器收到图片和费用落账仍是不同事实。

执行结果上限为每条 256 KiB、合计 32 MiB；新操作登记时为未完成/未知操作预留结果空间，容量不足在进入业务前拒绝。其余时钟、状态、行数及磁盘限制见 5.2。

`OperationStore.maintain()` 每次最多分类/删除各 100 条：过期 RUNNING 转 UNKNOWN；仅清理超过历史期限且 epoch 已退出有效期的 SUCCEEDED/FAILED 收据，再处理无未决操作、无被引用子记录的旧任务。活跃 epoch 收据和 UNKNOWN 不自动删除；旧 epoch 清理后仍不能用旧 key 恢复执行。维护调度、运维容量调优与生产回退演练留在 5.5，不自动部署维护任务。

## 隔离入口与验证边界

`python scripts/run_tiku_agent_phase5.py --help` 查看隔离入口。默认监听 `127.0.0.1:8910`，使用本 worktree 下 `.tmp_phase5_runtime` 和独立 Cookie `tiku_phase5_session`；拒绝已知生产目录/端口。该入口用于本地开发，没有对公网开放的部署承诺；启动后真实上传可能调用现有工具配置中的模型，本次验证仅使用 fake 工具和临时库，未启动服务。

CLI/飞书/旧 launcher 没有启用 attach_execution；共享 model_costs 的新增绑定默认关闭，不改原检索命令、章节、排序或费用账本行为。HTTP/Web 与 runtime 的相关入口已检查和接入；未扩展题库 Skill 的检索参数。

验证：`python -B -m unittest -q tests.test_execution_state tests.test_execution_operations tests.test_execution_frontend`，38 项通过。覆盖双独立进程抢占、重启登记、租约和执行截止、旧 writer、预先打开的 store、身份撤销、HTTP JSON/stream 重发、v6 fence/reset、换图/框/unit/候选及多题顺序冲突、A3 媒体失败收据、父子 attempt 外键、费用 run 归属、清理/容量、离线迁移与实际前端信封 helper。

上述证据完成 5.2/5.3 范围；不声称 A08/A10/A11/A12/A14/A15 的完整故障恢复、供应商费用恰好一次或整套 A01～A18 发布验收已通过。5.4 的副作用和费用对账、5.5 的真实浏览器/运行压力/迁移发布演练仍待完成。

最终全仓回归 `python -B -m unittest discover -s tests -p 'test_*.py'`：1504 项通过（90.160 秒）。两个新 CLI 的 `--help`、`node --check tiku_agent/demo_web/demo.js` 与 `git diff --check` 通过；未调用真实模型、未迁移 live 数据、未启动或重启任何服务。
