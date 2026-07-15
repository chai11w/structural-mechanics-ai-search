# Decision Trace Lab：可复现主线观测实验

这是仓库内隔离、可复现的主线 Agent 与网页 Demo 观测实验。它不重新实现搜题业务；左侧直接运行经 SHA-256 校验的主线 `tiku_agent` 代码，右侧只旁路记录和评审真实决策轨迹。

实验目录只提交源码、测试、规格与交接。`data/`、`runtime/`、人工 labels、SQLite、日志、题图和其他运行产物均不得提交。

目录中的编号 Markdown 是开发过程中的历史规格、验收与交接快照，保留当时事实，不代表当前工作树状态。当前入口、文件范围和验证命令以本 README 为准；历史报告中出现的旧错误、旧 Git 状态或已修复结论不得当作本次发布结果。

当前镜像来源、commit 和逐文件哈希记录在 `mainline_mirror/manifest.json`。启动前会校验全部镜像文件，缺失、被修改或未进入 manifest 的文件都会拒绝启动，不会回退旧私有 Agent。

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

右侧是独立评审侧栏。机器完整保存真实：

`turn_started → intent_decided / authorization_checked / tool_started / tool_completed / state_transition → turn_completed`

人工复核队列默认只展示：

- `intent_decided（意图判断）`
- `tool_completed（工具结果）`
- `turn_completed（回合完成／最终结果）`

只标想核对的关键项，不需要逐条评分；未标记保持 `unlabeled（未复核）`，不代表正确或错误。`NO_MATCH（未找到匹配）` 可另分 `reasonable_no_match / false_no_match / uncertain_no_match`，不会自动判错。

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
