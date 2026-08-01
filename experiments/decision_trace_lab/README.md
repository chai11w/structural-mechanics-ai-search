# Decision Trace Lab：可复现主线观测实验

这是仓库内隔离、可复现的主线 Agent 与网页 Demo 观测实验。它不重新实现搜题业务；左侧直接运行经 SHA-256 校验的主线 `tiku_agent` 代码，右侧只旁路记录和评审真实决策轨迹。

## 分支目的

题库目前只有约 800 道题，用户发来的题目没有匹配答案是正常情况，因此“有没有搜到题”不能直接代表 Agent 做得对不对。本分支不以提高命中数量为目标，而是收集真实主线每一轮的中间状态，让人工分别判断：意图是否正确、工具是否调用正确、工具结果是否合理、状态是否正确流转，以及最终结果是否符合本轮执行过程。

这些标记用于回答“问题究竟发生在哪一层”，为后续优化提供证据；它们不会自动修改 Agent，也不会影响展示主线。实验必须继续调用真实主线逻辑，不能用重新开发的简化流程代替主线测试。

实验目录只提交源码、测试、规格与交接。`data/`、`runtime/`、人工 labels、SQLite、日志、题图和其他运行产物均不得提交。

目录中的编号 Markdown 是开发过程中的历史规格、验收与交接快照，保留当时事实，不代表当前工作树状态。当前入口、文件范围和验证命令以本 README 为准；历史报告中出现的旧错误、旧 Git 状态或已修复结论不得当作本次发布结果。

当前镜像来源、commit 和逐文件哈希记录在 `mainline_mirror/manifest.json`。启动前会校验全部镜像文件，缺失、被修改或未进入 manifest 的文件都会拒绝启动，不会回退旧私有 Agent。

镜像通过 `scripts/sync_mainline_mirror.py` 从明确的 Git 提交原始 blob 重建，不从当前工作区复制。当前8793镜像指向已将全局搜索统一为“最终分（粗筛50% + 视觉复筛50%）≥95%”展示规则的8790主线提交 `04d3a86`。8793 默认使用同一安全回答生成器，右侧继续旁路显示回答方式和完整评审信息。

## 唯一有效网页入口

```powershell
Set-Location F:\cc\7-题库检索\experiments\decision_trace_lab
python app\run_mainline_observed_web.py
```

它只监听 `127.0.0.1:8793`，使用：

- 业务 runtime：`runtime/mainline_web`
- 轨迹和标签：`data/mainline_observed`
- Cookie：`decision_trace_mainline_session`
- 源码：`mainline_mirror/source`

它不读取主项目 config、生产 session/runtime/cookie/task log，不调用或代理 8790/8788，不启动 tunnel。真实题库只能通过显式外部配置作为只读数据源；答案输出、上传、媒体、SQLite 和任务日志均落在 Lab 独立目录。

## 左侧与右侧

左侧由镜像主线的 `tiku_agent.fastapi_demo.create_app` 和 `tiku_agent/demo_web` 原样提供，上传、stream progress、文案、候选按钮、答案、错误、reset 和媒体归属遵循同一 commit 的主线。

右侧是独立评审侧栏。普通评审先判断本轮最终回答：`正确 / 错误 / 部分正确 / 无法判断`。
每次点击都会立即保存，不存在额外提交步骤；再次点击当前选项会取消本轮评审。若把“错误”改成
“正确”，此前为该轮勾选的错误步骤会一起撤销，避免留下互相矛盾的隐藏标签。

只有最终回答不是“正确”时，页面才展开“可能出错的步骤”。普通界面默认只显示经过中文解释的：

- 最终意图判断成了什么；
- 失败或完成的关键工具得到了什么结果；
- 确实阻止过执行的处理规则。

新工具轨迹会直接显示工具名、`SUCCESS / NO_MATCH / NEEDS_INPUT / PARTIAL / TOOL_ERROR`
对应的中文状态、稳定原因码、是否可重试、下一业务状态和安全结果摘要；旧轨迹没有五态字段时仍按原
`ok` 值兼容显示。原始错误文本、凭据、路径、候选内容和模型原文不会进入评审轨迹。

日常错误归因只保留可判断的业务结果：成功路由显示“按数值荷载题检索/按字母荷载题检索”，结构分类显示具体类型，
复筛显示完成或明确的粗筛回退。正常成功的粗筛/全局候选数量和“不适用的结构筛选”不进入可勾选步骤；
它们的完整事件仍保留在技术详情。任何非成功五态仍进入错误归因，避免隐藏未命中、追问、降级或故障。

勾选步骤本身就会立即保存为可能出错点；错误原因可选，单独点击“保存原因”。机器字段、状态名、
来源、完整 JSON 和自动轨迹检查只放在折叠的“技术详情（开发排查用）”中，不占用日常评审界面。
不存在正文的事件保持空值，不保存或展示“正文未记录”这类占位文字。

机器轨迹仍完整保存真实：

`turn_started → intent_decided / authorization_checked / tool_started / tool_completed / state_transition → turn_completed`

只标想核对的步骤，不需要逐条评分；未勾选的步骤保持未复核，不代表正确或错误。
`NO_MATCH（未找到匹配）` 可另分 `reasonable_no_match / false_no_match / uncertain_no_match`，不会自动判错。

## 透明性边界

- `ObservedAgent` 调主线 `handle_image/handle_text` 恰好一次并原样返回同一结果。
- Intent 与授权 hook 调主线原函数恰好一次。
- `ObservedToolbox` 包装主线九个 callable，保持参数、结果对象、异常、次序和次数。
- 状态事件来自真实 `AgentState` 方法边界，不从最终 phase 反推。
- trace 写入、隐私拒绝、序列化或右侧 API 失败均不得影响主线返回。
- trace 不保存用户原文、绝对图片/答案路径、完整候选、prompt、模型原文或凭据。

## 验证

```powershell
Set-Location F:\cc\7-题库检索\experiments\decision_trace_lab
python -m unittest discover -s tests\mainline_parity -v
$env:PYTHONPATH = (Resolve-Path mainline_mirror\source).Path
python -m unittest discover -s mainline_mirror\source\tests -p "test_tiku_agent*.py" -v
node --check mainline_mirror\observation\web_static\observer.js
```

差分测试使用同一初始 `AgentState` 和同一确定性外部依赖，比较未观测主线、独立边界 spy 与正式 observed 版本的 `AgentResponse`、最终 state、intent/authorization 边界以及工具参数/结果/顺序/次数。HTTP 测试比较主线基准 App 与观测 App 的左侧 DOM、原 assets、session、普通/stream 文本、图片上传、reset 和安全行为。

## 未纳入的旧实现

`private_agent.py`、旧 `web_app.py`、`WebOfflineBackend`、`OfflineInjectedBackend`、`run_web_demo.py` 和 `run_web_demo_offline.py` 均不是主线 parity 证据，因此没有复制到这个可提交实验目录。
