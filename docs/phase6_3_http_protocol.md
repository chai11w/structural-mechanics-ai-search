# 阶段 6.3：独立 HTTP 任务与持久结果

状态：**6.3 DONE**，本批实现及验证完成。日期：2026-09-12。
开发分支：`codex/phase6-background-execution`；只在隔离 worktree 开发，尚未发布。
完整阶段约束见 [阶段 6 计划](phase6_execution_plan.md)，后台接收权威沿用 [6.2 内核](phase6_2_background_kernel.md)。

## 1. 本批范围与入口

`create_app(background_execution=True, ...)` 显式启用独立 HTTP 协议，默认关闭。
调用者必须预先组装带 `execution_dispatch` 的独立 A2/A3 runtime，显式传入 Feedback/Response Store、
`BackgroundInviteAccess`、独立 session Cookie 和 incoming 路径。
控制库、费用库、A2/A3 媒体、反馈案例、Response、Trace、Checkpoint 库及 Artifact 必须处于同一私有 runtime 范围，拒绝符号链接和越界路径。
登录 Cookie 与 session Cookie 不得相同，也不能使用原网页或管理员 Cookie 名。

本批没有给生产 launcher 增加后台开关，也没有修改飞书、CLI、题库检索与排序入口。
唯一随本批交付的启动脚本是离线 HTTP 探针，不打开端口、不加载生产配置或真实模型，不复用已有 runtime。
6.3 交付时网页仍使用原业务协议；后续已由 [6.4 网页恢复](phase6_4_web_recovery.md) 接入新协议，默认关闭模式保持原行为。

## 2. 协议 v1

所有任务对象均返回 `schema_version: 1`。接口需要有效登录 Cookie、已绑定 session 和匹配的所有权。
原操作键和 operation_id 都不是认证凭据；跨身份、会话、epoch、失效登录拒绝查询或订阅。
GET 查询使用 SQLite `query_only` 快照，不创建/续期业务会话、不领取任务、不刷新租约、不修复发布、不调用模型。
HTTP 请求仍可以写独立 Trace，这不改变业务权威。

| 方法与路径 | 输入 | 输出与行为 |
| --- | --- | --- |
| POST `/api/invite/login` | 原邀请码表单 | 签发带随机 nonce 的独立 Cookie；签发前登记有界登录记录 |
| POST `/api/jobs/session` | `X-Tiku-Background: 1` | 创建/绑定 session 与登录 grant；返回当前 `execution.epoch/state_version`，设置 session Cookie |
| POST `/api/jobs` | 下述 JSON 与提交头 | 持久提交后返回 202、`job.operation_id` 和 Location；仅承诺接收 |
| POST `/api/jobs/image` | 原始图片字节与提交头 | 校验完整图像后持久接收；不接受客户端文件路径 |
| GET `/api/jobs/lookup?key=...&epoch=...` | 原逻辑操作键与 epoch | 返回原操作或受控不存在/失效；不重新排队 |
| GET `/api/jobs/{id}` | operation_id | 当前持久状态、最新进度与 publication |
| GET `/api/jobs/{id}/stream?since=...` | 0～2^53−1 的进度游标 | NDJSON 最新快照，允许重新订阅；没有业务重放 |
| GET `/api/jobs/{id}/media/{mN}` | 返回结果提供的媒体 URL | 当前身份授权后读取冻结字节、核验摘要，返回 no-store 与 nosniff |
| POST `/api/invite/logout` | 有效登录 | 持久撤销本次登录及关联 grants，再删除 Cookie；重放旧 Cookie 无效 |

任务提交必须同时提供：

```text
X-Tiku-Background: 1
X-Session-Coordination-Version: 6
X-Session-Request-Fence: <当前毫秒时间戳>:<随机标识>
X-Tiku-Operation: {"key":"<原逻辑操作键>","epoch":"<当前 epoch>","state_version":<当前版本>}
```

POST/DELETE 检查同源；浏览器仍须遵守既有 Web Lock 要求，6.3 没有提供绕过版本检查或旧标签页保护的入口。
Fence 格式与有效时间沿用原协调协议。操作执行授权仍由 epoch、expected version、目标与原 execution writer 裁决。
任务提交 JSON 只接受 `kind`、`parameters` 两个顶层字段：

| kind | parameters |
| --- | --- |
| `handle_text` | `text`，可选 `action_context` |
| `select_unit` | `unit_id`、`task_revision`、`workflow_search_id` |
| `prepare_units` | `unit_ids`、`task_revision`、`workflow_search_id` |
| `handle_crop` | `bounds`（归一化 x/y/width/height）、`unit_id`、`task_revision`、`workflow_search_id` |

图片使用 `/api/jobs/image`，对应 `handle_image`。参数完整校验、输入容量及 producer 绑定沿用 6.2。
JSON HTTP 正文上限 32 KiB + 1 KiB 外层开销，持久参数仍上限 32 KiB；图片上限 15 MiB、4,000 万像素。
Content-Length 与实际累积字节均检查。未完整接收的正文不会进入派发库；接收提交后的 ACK 丢失不会撤销任务。
客户端应先保存逻辑键，ACK 丢失时按原键发现，必要时同键同体重试，不因连接超时生成新操作键。

常见受控状态：401 认证失效，404 找不到/结果不可用，409 协议或状态冲突，413 正文超限，
429 派发/订阅限额，503 容量不足或服务不可用。错误不返回内部路径、原文或异常 traceback。

## 3. 观察、结果和反馈

`job` 包含 operation_id、kind、status、dispatch_status、首次接收时间、原排队期限、受控错误码、
progress_version、固定词表 progress_stage 与独立后台 trace_id。
原排队预算保持 1 运行、2 排队、55 秒；观察或重发不延长截止时间。

`publication.status` 与业务状态分别解释：

| 状态 | 含义 |
| --- | --- |
| NOT_READY | 业务尚未成功结清，暂不可展示最终结果 |
| PENDING | 业务收据已保存，结果交付准备中 |
| READY | 公共结果、冻结媒体和唯一 Response ID 已关联 |
| FAILED | 有界结果准备失败；保留原业务收据，不重新搜索 |
| UNAVAILABLE | 发布结果已过期；不从当前题库补算历史结果 |

SUCCEEDED 表示本次业务方法已完成；选题、裁剪或等待补充也可能是该方法的正常结果，并不等于整个 workflow 答完。
READY 的 result 包含文本、图片引用、上传图/裁图、反馈 overlay、公开协议和历史 TaskState 快照。
`snapshot_role: historical` 与 `origin.operation_id/epoch` 明确来源，历史快照清空 allowed_actions。
后续动作使用当前 `/api/session`、`/api/execution` 等权威控制状态重新授权，不能直接执行历史按钮。

订阅帧为 `{"type":"snapshot","cursor":N,"resync":true/false,"data":{"schema_version":1,"job":...}}`。
游标断档或超前返回最新快照，不重放每条进度。正常消费时约每 250 ms 检查，30 秒后发送 reconnect 帧；客户端可再次连接。
终态与发布结果就绪后结束本次订阅。授权中途失效发送受控 error 并结束。
全局最多 64 个订阅、每 session 最多 2 个；生成器不建立进度队列，慢消费者只阻塞自身有限的快照传递。
所有订阅者断开后后台仍执行、准备结果、保存 Response；连接取消只结束 HTTP 观察作用域。

Response 的 trace_id 固定为 `trace_<operation_id>`、request_id 为 `req_<operation_id>`。
公共投影冻结业务返回时的 workflow/search/unit、A2/A3 路线、revision 等归属，按 trace_id 幂等写入。
重复观察不创建新 Response；原 `/api/feedback` 使用该 response_id，评分再次提交更新同一对象。
反馈图片从已授权的冻结媒体生成有界临时副本，复制到反馈案例后清理；不接受任意文件路径。

## 4. 持久交付与边界

执行库增加 publication schema v1：`execution_publications` 保存公共 payload、固定投影、状态和重试租约；
`execution_public_media` 保存媒体 BLOB 与摘要。业务响应仍持有原文件时先冻结结果，随后业务收据提交，再独立发布。
冻结失败不抹掉业务收据。后台 publisher 可按原 producer 与原收据重建结果；GET 不执行修复。
Response 提交后进程中断，重试同一投影取回同一 ID，不新增模型调用或费用。

发布最多 3 次，每次领取的占位 15 秒，已知失败最早 1 秒后再试；最后一次占位过期会结束为 FAILED，不永久挂 PENDING。
正常维护每次最多处理 10 个待发布、100 个过期项。
单媒体 ≤15 MiB，单结果 ≤32 个媒体，全部冻结媒体 ≤64 MiB，全部公共 payload/投影 ≤32 MiB，
单结果 payload/投影 ≤执行库既有的 256 KiB 上限，并检查磁盘余量。
发布保留期最多 30 天，同时不超过对应 Response 的到期时间；UNKNOWN 的证据不由本批过期清理。
反馈导出目录仅用于本服务生成的文件，上限 1,000 个/64 MiB，崩溃残留 1 小时后可清理。
登录登记与 grants 沿用 1,000 条容量门；到期绑定在下次登录/绑定时清理，注销不会绕过该门无限新增记录。

后台 Trace 与提交/观察 HTTP Trace 分开。持久 marker 与 Trace Store 唯一终态规则抑制重复终态；
观察断线不会把后台成功改成失败。Trace 仍是有界、尽力写入的诊断证据，marker 与 Trace 入库之间进程死亡可能丢诊断事件，
不承诺诊断事件恰好一次送达，也不以其代替业务收据。

旧业务 JSON/stream 路径在 background 模式下明确返回 409 BACKGROUND_PROTOCOL_REQUIRED；默认关闭时原接口保持原行为。
reset/停止等已有控制通道、会话读取、反馈继续沿用原版本与权限约束。
生命周期启动独立 worker 和 publisher；关闭时先停止接收/领取，分别给予 5 秒 drain。
未 drain 完不提前关闭在途依赖的 Trace/Checkpoint 采集器；完整强制退出、晚到调用收尾、版本升级和清理竞争仍由 6.5 验收。

## 5. 验证与后续

专项测试覆盖 A2 与真实 A3/A2 runtime 的五类操作，provider 可控且可计数，不调用付费模型。
真实 ASGI 消费链覆盖正文接收中断、接收提交后取消、排队/运行中断流、响应头后断流、两个慢消费者及超额订阅。
独立子进程在 Response 提交后使用 `os._exit` 中断，恢复仅修复交付、复用同一 Response。
另验证只读事务、原键发现、反馈/媒体、跨身份/会话/epoch/注销、容量、失效发布与旧接口拒绝。

```powershell
python -B -m unittest -q tests.test_background_http tests.test_background_http_a3 tests.test_background_admission tests.test_background_stream tests.test_background_publication
python -B -m unittest discover -s tests -q
python -B scripts/run_phase6_http_probe.py
```

最终全量回归：**1,695 tests / OK，161.744 秒，无跳过**，包含本批新增的 **26 项测试**。
日志：`.tmp_phase6_3/full-regression-final.log`；AST 解析及正常 Git 换行配置下的 diff check 通过。
最终探针：`.tmp_phase6_runtime/probe-f3c93f55023b4aaa87271f5dea235a8a/`，
1 次合成调用、1 个 attempt、1 条 Response，主动断流后 SUCCEEDED/READY，重复查询 3 次保持相同结果。
执行/Response/Trace 三库 quick_check 均正常，Trace 无重复终态，worker 与 publisher 均 drain 完成。
日志及探针数据不纳入提交；本批未使用真实模型样本，也未发布到生产。
本批为 F01/F03/F04/F06/F07/F09/F16 提供 HTTP 层证据，不宣称 F01～F18 全门完成。
6.4 的真实浏览器重连/刷新/多标签页证据见独立交付文档；后续 [6.5](phase6_5_lifecycle.md) 已补齐异常和重启矩阵，以及后台恢复控制 ACK 不重复创建评分回复的边界；6.6 再统一验收与获得发布授权。

后续完整阶段验收与 8898 独立发布已完成，当前发布与回退记录见 [6.6](phase6_6_acceptance_release.md)。以上本批记录保留原时点范围。
