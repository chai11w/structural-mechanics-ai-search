# 主线观测分线执行交接

## 1. 纠偏结果

本轮已停止把 `private_agent.py`、旧 `web_app.py`、`WebOfflineBackend` 或 offline-injected 成功当作主线效果证据。

新的唯一有效入口是：

```powershell
python app\run_mainline_observed_web.py
```

旧 `run_web_demo.py` 与 `run_web_demo_offline.py` 会直接 `SystemExit` 并说明已退役，不会静默启动旧 Agent。

## 2. 可验证主线镜像

来源：

```text
repository: F:/cc/7-题库检索
branch: main
commit: bc27cba1339f8a73aee18c4a44e109cecd84bd3d
```

快照前后主仓 `git status --short` 均为空。

`mainline_mirror/source` 包含 81 个受版本控制的代码、测试、fixture 和安全文档文件。排除了 `.git`、本地 config/密钥、Excel 题库资产、生产 runtime/log/cache、项目 Agent 元数据及 `special_unindexed_questions.json`。

`mainline_mirror/manifest.json` 记录来源、commit、创建时间及每个文件的 SHA-256。`activate_verified_source()` 在导入主线前逐项检查：

- manifest 必须包含 commit 和文件表；
- 文件必须存在且路径不能逃逸；
- SHA-256 必须一致；
- 不允许未进入 manifest 的额外源文件；
- 缺失或不匹配时抛 `SnapshotIntegrityError`，不会回退旧实现。

最终复验：

```text
SNAPSHOT_OK=81:bc27cba1339f8a73aee18c4a44e109cecd84bd3d
```

## 3. 直接执行的真实主线组件

8793 从 `mainline_mirror/source` 直接导入并运行：

- `tiku_agent.agent.TikuSearchAgent`
- `tiku_agent.agent.AgentToolbox`
- `tiku_agent.intent_v2.decide_intent_v2`
- `tiku_agent.action_permissions_v2.authorize_action_v2`
- `tiku_agent.state.AgentState`
- `tiku_agent.session_runtime.AgentSessionRuntime`
- `tiku_agent.fastapi_demo.create_app`
- `tiku_agent.demo_web` 的原 HTML/CSS/JS

没有复制或改写这些函数的业务内容；`mainline_mirror/source` 仍与 manifest 字节一致。

## 4. 透明观察边界

### 4.1 ObservedAgent

`ObservedAgent` 只在主线 `handle_image/handle_text` 外建立 turn，原方法恰好调用一次，返回原 `AgentResponse` 对象；异常保持原类型和边界并原样抛出。

### 4.2 Intent 与授权

`HookManager` 安装在主线实际使用的模块引用上：

- `agent.decide_intent_v2`：原函数一次，记录真实最终 `ActionDecisionV2`，原对象返回；
- `intent_v2.authorize_action_v2`：原函数一次，记录真实 decision/context/result 摘要，原对象返回。

授权次数不固定。每回合 `turn_completed.authorization_count` 保存实际调用数，scan 比较实际计数和已写事件数；真实零次不会被人造补齐，真实调用记录缺失会产生 `authorization_trace_count_mismatch`。

### 4.3 工具

`ObservedToolbox` 包装主线 `AgentToolbox` 的九个 callable。每次记录 `tool_started/tool_completed`，保持原参数、调用顺序、次数、ToolResult 对象、异常和重试行为。输入与输出只记录白名单数量和枚举摘要，不记录图片路径、完整候选或答案路径。

### 4.4 状态

状态对象始终是主线 `AgentState`。透明 class-method hook 在真实状态方法前后读取脱敏摘要并记录 diff；事件包含真实 trigger、phase 前后及变更字段。没有从最终 phase 拼装状态轨迹。

所有 hook 通过 `ContextVar` 绑定当前 turn；未处于 observed turn 的同进程主线调用不会写轨迹。trace 写失败、隐私拒绝或 serializer 异常在观察边界吞掉，不参与业务判断。

## 5. Web 主线一致性

Observed App 先调用镜像主线 `fastapi_demo.create_app` 创建完整业务 App，再只增加：

- 外部 Cookie `decision_trace_mainline_session` 与主线内部 Cookie 的透明翻译；
- `/api/observation/source|turns|labels|summary|scan`；
- 根页面 `</body>` 前的右侧 aside、独立 observer CSS/JS；
- 8793、`127.0.0.1` 和 Lab 独立路径。

原 `demo_web/index.html` 的左侧 DOM、文案和 ID 未修改；`/assets/demo.css` 与 `/assets/demo.js` 逐字节来自同一镜像。候选按钮仍由主线 JS 发送 `选择候选 ${index + 1}`。观察 markup 可剥离后与 baseline 根 HTML 精确相等。

隔离位置：

```text
runtime/mainline_web/session.db
runtime/mainline_web/sessions
runtime/mainline_web/incoming
runtime/mainline_web/task_logs.jsonl
data/mainline_observed/traces.jsonl
data/mainline_observed/labels.jsonl
data/mainline_observed/diagnostics.jsonl
```

未启动服务，未触碰 8790/8788、生产 session/runtime/cookie、tunnel 或题库写操作。

## 6. 右侧轨迹 UI（按 16 补充）

默认人工复核队列只筛出：

- `intent_decided（意图判断）`
- 实际数量的 `tool_completed（工具结果）`
- `turn_completed（回合完成／最终结果）`

技术详情默认折叠并保留全部 sequence：`turn_started`、实际授权事件、`tool_started`、`state_transition` 等均未停止采集。

界面明确说明“只标想核对的关键项，不需要逐条评分”，只显示 `关键项 X 条 / 已复核 Y 条 / 其余 unlabeled`，没有完成率、必填、固定十条或阻塞下一回合。

事件与九个工具均使用 `英文（中文解释）`。未知值保留英文并显示 `未知事件/未知工具`。事件数量按真实数组动态渲染。

scan issue 会在顶部提醒并自动展开技术详情。NO_MATCH 最终卡显示三分类，默认不标红、不自动写标签。`labels.jsonl` 仅保存用户实际点击的标签。

窄屏 `max-width: 900px` 时不压缩/改写主线左侧 `.app-shell`，侧栏改为独立抽屉；桌面仅为左侧留出侧栏宽度。

## 7. Agent 差分证据

`tests/mainline_parity/test_agent_parity.py` 对每个场景运行三路：

1. 未安装观察 hook 的业务 baseline；
2. 不写正式 trace 的独立边界 spy reference；
3. 正式 HookManager + ObservedAgent + ObservedToolbox。

逐项比较：

- AgentResponse text/images/state/intent；
- 最终 `AgentState.to_dict()`；
- Intent 决策摘要序列；
- 授权次数、参数摘要和结果序列；
- 工具名称、参数摘要、完整 fake ToolResult、顺序和次数；
- 异常类型、消息和发生边界。

覆盖 23 个确定性业务场景/分支：章节未知、章节后检索、自动章节、候选取答案、多题及题号选择、章节纠正、拒绝候选、继续搜索、回看候选、重发答案、答案不匹配、greeting/small talk/help、模糊编号、LLM 兜底、未授权/已授权 global_search、NO_MATCH 与恢复、复筛不完整、cancel/retry/explain_failure，以及工具异常。

另注入 recorder `append_event` 抛 `OSError`，业务响应、状态和工具序列仍与 baseline 完全相同。

## 8. HTTP/前端差分证据

`tests/mainline_parity/test_web_parity.py` 比较同一依赖下的主线基准 App 与 observed App：

- 根 HTML 去除唯一观察块后精确相等；
- 主线 CSS/JS assets 字节相等；
- `/api/session` 主线字段和值相等；
- 普通文本与 NDJSON stream 状态码、progress/result 相等；
- PNG multipart 图片的 text/images/intent 相等，双方保留各自合法随机 upload URL；
- reset 相等；
- 空图片、非法图片与 media traversal 拒绝相等；
- 外部 Cookie 独立；
- source API 能证明 commit 与 81 文件校验；
- 右侧双语、动态关键队列、标签和异常提升可用。

## 9. 测试结果

镜像差分：

```text
Ran 11 tests
OK
```

其中完整 Agent 矩阵位于一个带 23 个 subTest 场景的测试中，不以顶层 unittest 数量代替覆盖说明。

主仓同 commit、短路径原测试：

```text
python -m unittest discover -s tests -p 'test_tiku_agent*.py' -v
Ran 164 tests
OK
```

直接从深层镜像目录运行同一 164 项时 159 项通过，5 项仅在 Windows 创建测试 artifact 的 64 位 session hash 子目录时超过传统 260 字符路径限制；失败均为 `WinError 206`，没有业务断言失败。为保持镜像字节一致，没有修改主线测试或业务源掩盖该环境限制；同 commit 在短路径主仓 164 项全部通过。

静态验证：

```text
node --check observer.js: OK
py_compile: OK
manifest verify: 81 files OK
```

## 10. 最终边界

- 主仓没有修改，最终 `git status --short` 为空。
- 没有启动任何后台服务。
- 没有请求、停止或重启 8790/8788。
- 没有读取生产 config/session/runtime/cookie。
- 没有复制 Excel 题库资产或执行入库、删除、修复。
- 默认入口、README 和 parity 测试均不 import 旧 private/offline Agent。
