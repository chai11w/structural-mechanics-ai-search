# 阶段 6.2：持久接收与后台执行内核

状态：**6.2 DONE**。日期：2026-09-12。分支：`codex/phase6-background-execution`。

实现范围：私有 Python 内核、A2 图片/文本、A3 图片/文本/选题/准备/裁剪、后台线程生命周期和离线探针。
HTTP 提交/订阅、公共 Response/媒体发布和浏览器恢复留在 6.3/6.4；本批不能作为网页断线功能已上线的声明。
总体约束见 [阶段 6 契约](phase6_execution_plan.md)。

## 1. 接收和执行边界

`DispatchStore.accept` 接受稳定 session、identity、服务端 grant、原 operation envelope、操作类型和参数。
`freeze_input` 限定五个既有业务方法，按原签名补默认值，沿用现有 operation fingerprint。
图片仅接收有界字节，验证 PNG/JPEG/WebP 和像素上限；拒绝客户端路径、可执行对象以及任意额外参数。
文本、typed action、workflow/revision/unit、裁剪 bounds 等完整参数进入私有存储。

同一 `ExecutionStore.transaction` 内完成授权、session/epoch/version、费用、记录/字节/队列容量检查，
登记 operation、dispatch 和 input，然后提交。`accept` 只有退出外层提交后才能返回接收凭据；禁止在调用者尚未提交的同库事务中调用。
提交失败不返回接收成功。提交成功但 ACK 丢失时，按原键 `observe` 找回；同键重发沿用原截止时间，异体同键拒绝。

`BackgroundWorker` 自己持有线程、心跳、writer、效果和结果收据，不持有 Request、上传对象、Cookie 明文或客户端事件循环。
`claim_next` 在同一事务里重验输入摘要、producer、授权、期限和额度，再通过原 `OperationStore.claim` 领取。
派发领取和 attempt/token 原子落库，没有第二套业务执行状态。原 runtime 方法在已领取 writer 内执行，A3 子调用继承原费用归属。
返回需要人工补充、选题或裁剪时，本次 operation 正常完成，worker 不擅自生成下一条用户指令。

## 2. Schema 1

所有表在隔离的 execution 数据库，使用同一事务域与外键。

| 表/元数据 | 保存内容 | 权威与生命周期 |
| --- | --- | --- |
| `execution_dispatch_grants` | 随机服务端 grant ID、session/identity 摘要、auth_version、expires、revoked | 登录授权引用，不是认证凭据；由可信入口建立和撤销 |
| `execution_dispatch` | 一对一 operation_id、grant_id、WAITING/CLAIMED/SETTLED/REVOKED、首次接收/排队截止时间、固定错误码、最新进度阶段/单调版本 | 只负责派发；业务成败与 UNKNOWN 仍读 execution_operations |
| `execution_dispatch_inputs` | 完整 session/identity、规范 JSON、JSON 摘要、可选图片 BLOB/摘要、字节占用 | 私有执行数据；不进入 Trace/Checkpoint/公开状态；operation 删除时级联删除 |
| `execution_meta.dispatch_schema` | `1` | 未识别版本拒绝启动 |
| `execution_meta.dispatch_policy` | 规范化容量和 TTL 配置 | 同库 worker 必须使用相同配置；禁止不同实例各自扩大队列 |

图片 BLOB 与指令一起提交，接收阶段不创建文件 staging，因此不存在“已 ACK 但图片还没落盘”的独立文件提交窗口。
领取后在 execution 数据库所属 runtime 的专用子目录生成 `operation_id.token.ext` 文件，以独占创建、fsync 和摘要复核完成物化。
runtime 按原规则保存业务图片；物化文件在真实执行结束后删除，进程崩溃后的孤儿由维护扫描处理。
原文和图片不是公开结果接口。内核 `observe(result=True)` 只供可信服务层读取业务收据；禁止直接序列化到 HTTP。

## 3. 冻结的默认限额与清理规则

| 项目 | 默认值 |
| --- | --- |
| 外层执行并发 / 等待队列 / 首次等待期限 | 1 / 2 / 55 秒 |
| 派发记录 / grant 记录 | 各 1,000 条 |
| 单 JSON / 单图片 | 32 KiB / 15 MiB |
| 图片像素 | 4,000 万 |
| 私有输入总字节 | 64 MiB |
| 已结清输入保留 | 终态派发更新时间后 24 小时 |
| grant 最大年龄 | 30 天，且不超过登录入口给出的真实过期时间 |
| 业务结果 / 数据库 / 磁盘余量 | 继续使用 ExecutionPolicy 的 256 KiB 单结果、32 MiB 总结果、256 MiB 数据库及 256 MiB 最小余量 |

接收前核算 BLOB/JSON/身份字节，检查数据库、WAL 和物化空间；容量不足拒绝接收，不能淘汰有效排队/运行/UNKNOWN 指令腾位。
新的外层限额不能超过 runtime 已配置的限额。新内核只附着到空闲的隔离 runtime；该库启用 dispatch 后，旧业务变更入口拒绝绕过接收内核。
reset/控制/显式证据恢复沿用现有独立控制通道。A3 内部并发继续走原有受限 gate。

后台调度从持久队列选择任务，不预先把全部队列塞进 runtime gate；占用同一 runtime 条带锁的任务继续留在持久队列。
同 session/epoch 未完成操作继续互斥。超时未开始的 operation 标记 FAILED，派发 REVOKED，原键不会重新排队。
出队授权检查消耗的时间也计入原 55 秒，不再给一段新的等待预算。

维护按批执行，每批最多清理 100 条已结清私有输入及 100 个无关联的过期/撤销 grant；物化目录扫描有界。
WAITING、RUNNING、UNKNOWN 的有效输入受保护。业务收据和派发墓碑沿用原 operation 历史保留与安全删除条件。
未确认效果/费用/文件依然阻止历史淘汰；长期未知数据占满容量时拒绝新任务，需要按现有证据恢复/人工核对规则处理。

## 4. 授权和模型发送

`create_grant` 是可信登录集成接口，不能暴露为允许浏览器自行填写 identity/version/expiry 的接口。
调用者必须先完成真实登录认证，再传入稳定身份、登录版本及真实到期时间。grant 不保存 Cookie、邀请码、签名或 API key。
内核要求提供有界、实时、只读的 `authorize(identity, auth_version)`，仅明确返回 `True` 才允许。
生产集成应从 `SQLiteControlStore.active_invitation` 检查禁用/删除/到期/版本变化；读取失败按 AUTH_UNAVAILABLE 拒绝。
测试探针使用显式的本地替身授权器，不连接生产控制库。

接收、领取、模型 prepare/send 均检查：grant、身份版本、session/epoch、额度及待对账。
`revoke_grant` 对应显式注销；关闭浏览器不撤销 grant。注销后的新观察、领取和模型发送均被阻止。
已经发送的调用继续保留自己的响应证据和费用；授权撤销不会抹除已经发生的调用。
HTTP Cookie 与 grant 建立/注销的实际路由衔接属于 6.3/6.5。

## 5. 状态与关闭

`observe` 核对调用方 session/identity/epoch 和 grant，读取原操作状态、最新固定阶段及版本；不领取、续租、调用模型或延长会话。
结果成功时复用原业务收据和文件摘要；重复读取不新建费用或业务结果。进度只存固定阶段，不存任意进度文案。

`start` 启动有界 worker；`close` 停止本实例接收和新领取，等待指定 drain（0～60 秒）并返回真实 drained/running_workers。
未退出的同步 provider 不能被线程取消，仍由原 writer 管理直到真实退出；调用方必须处理 `drained=False`，不能据此宣称完成发布。
重启只领取完整、有效且从未领取的 WAITING 指令；不延长排队期限。
已领取租约失效分类 UNKNOWN，原输入继续保留，绝不改回 WAITING 自动发送第二次模型调用。
业务提交后 worker 未来得及结清派发，维护按原 SUCCEEDED 收据结清，不重新执行业务。

本批提供恢复未领取队列与保守分类的内核；完整服务 crash/drain/版本切换和前端交付窗口仍由 6.5/6.6 验收。
schema 为加表式变更，但不批准把启用了后台队列的数据库交给旧版本运行。新版本 producer/策略不同则拒绝领取；部署前需要排空或明确迁移。

## 6. 验证与交付边界

新增测试：

- [接收、领取、权限与费用](../tests/test_execution_dispatch.py)：A2 两类操作、事务回滚、幂等/并发领取、容量/期限、后台无观察者执行、受控关闭、撤权/发送、未知效果、私有载荷与清理。
- [A3 五类操作](../tests/test_execution_dispatch_a3.py)：真实 A3/A2 runtime、人工裁剪、并行逐题准备、选题复用、父子单 attempt 和精确费用收据。
- [提交和进程中断](../tests/test_execution_dispatch_durability.py)：子进程提交前退出、提交后 ACK 前退出、嵌套事务拒绝、共享容量、出队期限、输入/记录限额与私有路径保护。

离线探针：

```powershell
python -B scripts/run_phase6_kernel_probe.py
```

只创建当前 checkout 的 `.tmp_phase6_runtime/probe-<随机 ID>`，使用合成 provider，不打开服务端口，不读取私有配置或 live 题库。
可指定该目录内的新路径；生产 runtime、旧飞书路径、目录穿越、已存在目录均拒绝。
独立 execution/费用/媒体/物化路径和 `report.json` 均留在该目录；没有 Cookie 或 HTTP 服务。

F01/F02/F03/F04/F05/F11/F15 的**内核部分**有上述新增证据，不能据此把涉及真实 HTTP 断线、浏览器、公共媒体、Trace、发布的整个 F01～F18 矩阵标成通过。
最终验证：

- 三个新增模块共 **30 tests / OK，6.711 秒**；日志 `.tmp_phase6_2/kernel-tests-final.log`。
- 全量 `python -B -m unittest discover -s tests -q`：**1,669 tests / OK，136.643 秒，无跳过**；日志 `.tmp_phase6_2/full-regression-verified.log`。
- 最终探针目录 `.tmp_phase6_runtime/probe-6788dfa34e1142279a03b11f9f85d653/`：1 个 operation、1 个 attempt、1 次合成调用、13 tokens、费用 CONFIRMED；重复读取 3 次，关闭 drained=True。
- 探针 execution/费用 SQLite 均 `quick_check=ok`，连接同步模式 FULL；10 个 Python 文件 AST 和 6 个相对文档链接核验通过。

验证输出均不提交。主分支仍为 `c5ec463`，本批仅在隔离 worktree 修改和本地提交；没有发布到生产。
