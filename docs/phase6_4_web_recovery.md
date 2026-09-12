# 阶段 6.4：网页后台任务恢复

日期：2026-09-12。分支 `codex/phase6-background-execution`。
6.4 实现与隔离浏览器验收已完成，按模块隔离的全量测试通过。单进程全量回归存在下述 Trace 排空时序失败，不能称为单进程全绿。仅本地开发，未合入主线、推送或发布。
完整阶段约束见 [阶段 6 计划](phase6_execution_plan.md)，服务端协议见 [6.3 交付](phase6_3_http_protocol.md)。

## 范围与入口

- `create_app(background_execution=True)` 为 HTML 加入服务端模式标记；网页据此使用 `background_jobs.js`。默认关闭时仍使用旧业务协议。生产 launcher 未新增后台开关。
- A2 文本/图片以及 A3 选题、批量准备、手动裁剪全部经 `/api/jobs` 或 `/api/jobs/image` 提交。A3 的 workflow、unit、revision 原样映射到 6.3 参数。
- CLI、飞书、检索识别、章节边界、排序、费用、派发内核和控制命令协议保持原有入口；本批不改变其业务逻辑。README 增加隔离开发入口说明。
- 后台模式可查看任务状态与既有 reset/停止/结束/核对控制。展开面板参与页面布局，不遮挡候选按钮；默认模式仍隐藏该面板。

## 持久接收与恢复

1. 业务提交持有既有 session Web Lock，重验当前 epoch/version，并绑定独立后台登录 grant。绑定只发生在用户明确提交时。
2. 在业务 POST 前，把操作键、epoch/version、kind、operation_id、最新进度版本和交付标记保存到 `localStorage`，写入后读回核验。正文、图片、Cookie、grant 不写入操作记录。
3. ACK 到达后关联原 operation_id；ACK 丢失保留原键和 pending fence。释放提交锁后，查询原键或原 operation_id。恢复路径没有业务 POST。
4. 运行中刷新先读取原任务，不先等待旧 runtime 的长会话锁。操作键仅为查找线索，登录、session 和 epoch 仍由服务端 GET 校验。结果可读后再读取当前会话授权。
5. 本版选择**最新完整快照轮询**：每个观察者最多一个在途读取，正常间隔 1 秒，单次 HTTP 最多 15 秒；保存单调进度版本，不重播中间进度，也不占用两个 NDJSON 订阅席位。快照版本跳跃直接采用最新权威结果。
6. 临时读取失败按 1/2/4/8 秒退避，连续 5 次失败后保留记录并提示重新连接。online 事件、手动重连、重新打开网页都只查询原任务。
7. 401 停止并提示重新登录；原键 404 保留不确定记录，不凭超时生成新键。无法证明接收的任务可再次核对或显式开始新对话；本版不持久保存私有输入用于自动/后台重传。
8. 操作记录最多 64 条；新提交前压缩已交付收据，通常保留最近约 50 条。未核对项不静默删除，存储失败或容量不足时停止新提交。

本地时间不裁决服务端会话失效。查询不绑定新 grant、不延长排队期限、不调用模型；服务端拒绝的旧 epoch 只保留其记录，不阻塞当前会话的权威读取。

## 展示与动作边界

- 业务 `SUCCEEDED` 和 publication `READY/PENDING/FAILED/UNAVAILABLE` 分开显示；`UNKNOWN` 提示核对，不自动重试。发布失败不通过重新搜索补偿。
- READY 必须同时匹配原 operation_id、epoch、historical 标记及稳定 Response ID。历史 TaskState 从不送入当前状态 consumer。
- 当前 `/api/session` 读取成功后才更新动作权限。历史按钮保留原 workflow/unit/revision/candidate generation，继续由现有 branded 动作校验和服务端 CAS 判断能否执行。
- 回复按 operation 和 Response 去重，message_id 稳定。跨标签页交付使用独立 Web Lock 合并共享历史；同 epoch 的已发布回复在另一个标签页输入时保留。历史持久化成功后才标记交付完成。已完成记录不因历史正常裁剪而重新查询，遵守原有 50 条历史保留边界。
- 修复旧 inline 消息过滤对后台选题回复的误删；后续文本结果不会重复显示此前提交的裁图。冻结媒体 URL 可用于刷新后的图片展示和反馈。
- 重复评分绑定同一 `rated_response_id`。反馈是独立请求，不要求业务 pending fence，也不会更新 TaskState；业务请求缺少 fence 仍拒绝。
- reset、跨标签页重置和新的会话 generation 使旧观察失效。旧任务晚到的结果、错误提示及 busy 收尾均不得覆盖新对话。无 Web Lock 时可读取历史，但不能提交任务或控制命令。

## 验证证据

客户端契约：`tests/phase6_background_client_checks.js` 的 12 项检查，由 `tests.test_background_frontend` 纳入 unittest。
覆盖 ACK 丢失后重建客户端、断网、原键未找到、注销、epoch 切换、结果错绑、存储失败、无 Web Lock、交付落盘失败、并发观察、UNKNOWN 和五类命令参数。

单进程全量回归运行 1696 项（242.818 秒），1695 项通过、1 项报错：`test_verifier_matches_the_real_fastapi_contract_without_a_port` 的既有 Trace smoke 要求写入队列在固定 200 毫秒窗口后 `pending <= 1`。源文件在该轮运行期间未改变；该项随后单独运行通过（2.081 秒）。本批没有改动 Trace 写入实现、放宽断言或增加该等待窗口；失败与运行方式相关，具体根因尚未确定。前端相关 19 项测试、两个 CLI 帮助入口及 JS 语法检查通过。

随后使用独立 Python 进程逐模块运行同一批测试：154 个模块、1696 项全部通过，无跳过（378.453 秒）；其中 Trace 验证模块 25 项全部通过。汇总保存在 `.tmp_phase6_4/isolated-regression-summary.json`，每个模块的原始输出为 `isolated-test*.log`。这证明全部测试在模块隔离方式下通过，不替代上述单进程失败记录。

真实 Chromium 使用独立临时 SQLite、媒体和生成的测试登录；合成 provider 可计数，没有付费模型或 live 题库数据。测试服务监听系统分配的 loopback 端口，未使用 8788/8790/8795/8888 或生产配置。

| 验收入口 | 证明 |
| --- | --- |
| `phase6_browser_acceptance.js` | 运行中刷新、第二标签页、终态刷新、断网、ACK 丢失、关闭任务页面、ACK 丢失后刷新、无 Web Lock、过期页面输入与共享回复保留；每个明确业务命令只有一个调用和 attempt |
| `phase6_a3_browser_acceptance.js` | 上传 → 选题 → 手动裁剪 → 补章节 → 候选 → 答案；刷新保持 Response 顺序/ID、两次评分同一 Response、裁图不重复、已消费候选失效、旧标签页 reset 后零新操作、390px 无横向溢出 |
| `phase6_auto_browser_acceptance.js` | 页面调用 1 次、两个选中 unit 校验各 1 次；prepare 操作 1 个；刷新零额外调用、稳定回复 2 条 |
| `phase6_running_reset_browser.js` | 模型调用在途时 reset，调用只增加 1 次；晚到结果和提示不入新对话，刷新不复活旧 epoch |
| `phase6_history_retention_browser.js` | 已完成收据不绕过历史保留；删除的历史回复不复活，刷新增加 0 次任务读取 |

本地证据位于 `.tmp_phase6_4/`：`recovery-browser-final.log`（19 项断言、6 个业务操作仅增加 6 次调用和 6 个 attempt）、`a3-browser-final.log`（9 项断言）、`auto-browser.log`、`running-reset-browser.log`、`history-retention-browser.log`、`full-regression-final.log`。移动端截图为 `output/playwright/phase6-4-mobile.png`，已视觉检查。原始日志、媒体和运行数据库不提交。

复验命令（工作目录为本 worktree）：

```powershell
node tests/phase6_background_client_checks.js
python -B -m unittest -q tests.test_background_frontend tests.test_demo_web_task_state tests.test_execution_frontend
python -B -m unittest discover -s tests -q
# 按模块隔离复验；每个模块启动独立进程，遇到失败立即报错。
Get-ChildItem tests -Filter 'test*.py' -File | Sort-Object Name | ForEach-Object {
    python -B -m unittest -q ('tests.' + $_.BaseName)
    if ($LASTEXITCODE -ne 0) { throw ('Failed: ' + $_.Name) }
}
python -B -m tests.phase6_browser_fixture
python -B -m tests.phase6_a3_browser_fixture
python -B -m tests.phase6_a3_browser_fixture --automatic
```

浏览器夹具启动后输出自身端口与合成图片路径。把合成图片复制到 `.tmp_phase6_4/synthetic-page.png`，通过独立命名 Playwright 会话打开该端口的 `/fixture/start`，使用 `playwright-cli run-code --filename tests/<上述脚本>` 验收。夹具没有生产启动用途；结束后仅停止本次创建的精确进程。

## 后续边界

本批为 F08 提供网页和真实浏览器证据，补充 F01/F04/F07/F09/F16 的前端证据，不宣称 F01～F18 全门完成。
6.5 仍负责完整崩溃/重启、UNKNOWN 的安全本地恢复、撤销/过期/清理竞争和版本切换矩阵；6.6 负责统一验收、真实样本范围和可回退发布。阶段 7 暂停/继续仍延期。
