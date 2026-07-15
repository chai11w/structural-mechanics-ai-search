# 主线观测镜像独立验收报告

## 结论

**PASS**

本轮验收口径是主线 parity，而不是“8793 能运行”。独立证据表明：有效入口执行同一 commit 的主线镜像，观测层没有改变 AgentResponse、AgentState、Intent、授权、工具序列或 HTTP 左侧行为；旧 private/offline Agent 已退出有效入口。

## 1. 镜像来源与启动完整性

**通过。**

- 主仓实测为 `main@bc27cba1339f8a73aee18c4a44e109cecd84bd3d`，与 manifest 一致，工作树为空。
- `manifest.json` 共 81 个文件；逐项计算主仓文件、镜像文件和 manifest SHA-256，结果 `MISMATCH=0`。
- 测试结束后再次执行完整校验：`SNAPSHOT_OK=81:bc27cba...`。
- 独立篡改注入让 `tiku_agent/agent.py` 的读取字节发生变化，`verify_snapshot()` 明确抛出 `SnapshotIntegrityError` 并报告 hash mismatch；没有回退旧实现。
- 完整性实现同时拒绝缺失文件、逃逸路径和未进入 manifest 的额外源文件。

## 2. 有效入口与真实导入图

**通过。**

唯一有效入口 `app/run_mainline_observed_web.py` 创建 `create_observed_app()`。在独立 Python 进程导入该入口后，以下模块的实际 `__file__` 全部位于 `mainline_mirror/source`：

- `tiku_agent.intent_v2`
- `tiku_agent.action_permissions_v2`
- `tiku_agent.agent`（含 `TikuSearchAgent`、`AgentToolbox`）
- `tiku_agent.state`（`AgentState`）
- `tiku_agent.session_runtime`
- `tiku_agent.fastapi_demo`

该进程中 `private_agent` 和旧 `web_app` 均未进入 `sys.modules`。左侧 HTML/资产由镜像主线 `demo_web` 提供。

两个旧启动器均实际执行验证，退出码为 1，并明确提示“已退役”；`run_web_demo_offline.py` 不会启动 `WebOfflineBackend`。README 只推荐新的主线镜像入口。

默认短 runtime 的 TestClient 启动、根页面、独立 Cookie 和一次真实主线文本请求均成功；`/api/observation/source` 返回同一 commit 与 81 个已验证文件。

## 3. Agent 三路差分与透明性

**通过。**

镜像 parity 套件：`Ran 11 tests, OK`。其中 Agent 矩阵对 23 个 subTest 场景逐一运行：

1. 未安装观察 hook 的 baseline；
2. 独立 boundary spy reference；
3. 正式 HookManager + ObservedAgent + ObservedToolbox。

逐场景相等项包括：

- `AgentResponse.text/images/state/intent`；
- 最终 `AgentState.to_dict()`；
- Intent 与授权真实边界序列；
- 工具名称、参数摘要、完整 fake ToolResult、次序和次数；
- 异常类型、消息和发生边界。

23 场景覆盖章节未知/确定、多题与题号选择、章节纠正、候选答案、拒绝与继续搜索、回看与重发、答案不匹配、问候/闲聊/help、模糊编号、LLM 兜底、未授权/已授权 global search、NO_MATCH 及恢复、复筛不完整、cancel/retry/explain_failure。

代码复核确认 ObservedAgent 对主线 `handle_image/handle_text` 只调用一次；Intent、授权和九个工具 wrapper 均只调用原函数一次并原样返回。状态轨迹来自主线 `AgentState` 方法前后 diff，不是根据最终 phase 拼装。

工具异常测试确认 baseline 与 observed 在 `rerank_candidates` 边界抛出相同 `RuntimeError`，状态和此前工具调用一致。recorder `append_event` 注入 `OSError` 后，响应、状态和工具序列仍与 baseline 完全相等。隐私拒绝、payload 序列化及诊断写入异常也位于 `_safe_emit`/存储 fail-open 边界，不参与业务决策。

## 4. 主仓 164 项与镜像深路径评估

**通过。**

- 主仓同 commit 短路径：`Ran 164 tests, OK`。
- 深层镜像目录：`Ran 164 tests, FAILED (errors=5)`，其余 159 项通过。
- 5 个错误全部是 `SessionArtifacts`/`AgentSessionRuntime` 测试在深目录创建 64 位 session hash 子目录时触发 Windows `WinError 206`；堆栈均停在 `pathlib.mkdir`，尚未进入业务断言。
- 同一字节、同一 commit 在短路径主仓 164/164 通过；Lab 的较短 `runtime/mainline_web` 又完成实际 App、Cookie、会话数据库和文本请求。因此这 5 项被判定为测试 cwd 的 Windows 路径长度环境限制，不是可忽略的业务失败，也不阻断独立短 runtime。

没有修改镜像源码或主线测试去掩盖该限制。

## 5. HTTP 与左侧主线 parity

**通过。**

- 去除唯一 observer 注入块后，observed 根 HTML 与 baseline 精确相等。
- `/assets/demo.css`、`/assets/demo.js` 与主线镜像逐字节相等；候选按钮仍发送 `选择候选 ${index + 1}`。
- `/api/session` 原字段和值相等。
- 普通文本、NDJSON stream 的状态码、progress 和 result 相等。
- PNG multipart 的 text/images/intent 相等；随机 upload URL 只做合法隔离差异。
- reset、空/非法图片、媒体路径穿越拒绝相等。
- 补充实测 `MAX_IMAGE_BYTES + 1`：baseline 与 observed 均返回相同 413 响应。
- 观察层只新增右侧 markup、observer assets/API、独立 Cookie 和隔离路径；没有删除或改名主线接口字段。
- observer CSS 在桌面仅给右侧栏让位；900px 以下恢复主线 `.app-shell` 的 `right: 0`，侧栏成为独立抽屉，不改写主线移动端 CSS/JS。

本轮没有运行长时间浏览器；TestClient 差分、DOM 精确比较、资产字节比较、JS 语法检查和 CSS 静态复核已经覆盖本次必要 parity，避免把浏览器工具稳定性混入业务结论。

## 6. 右侧轨迹与人工复核 UI

**通过。**

- 原始 trace 保留完整英文 `event_type` 和真实 sequence。
- 右侧事件、九个工具均显示 `英文（中文解释）`；未知值保留原值并显示“未知事件/未知工具”。
- 默认可评队列严格筛选 `intent_decided`、实际数量的 `tool_completed`、`turn_completed`。
- `turn_started`、真实数量的 `authorization_checked`、`tool_started`、`state_transition` 位于默认折叠的完整技术详情中，仍可查看脱敏 payload。
- 关键项和事件数量从真实数组动态计算，没有固定 10 条、完成率、必填或阻塞下一回合。
- scan issue 会显示 `英文 code（中文异常解释）` 并自动展开技术详情；授权计数与真实记录不一致、状态检查失败、序列和工具配对异常均可被 scan 发现。
- NO_MATCH 最终卡提供 reasonable/false/uncertain 三分类，默认保持 unlabeled，不自动判错。
- 只有用户点击才写 label；标签和 observer API 失败不会阻断左侧。

## 7. 隔离与最终状态

**通过。**

- 业务 runtime：`runtime/mainline_web`；轨迹/标签：`data/mainline_observed`。
- 外部 Cookie：`decision_trace_mainline_session`，并与主线内部 Cookie 做透明转换。
- 默认入口固定 `127.0.0.1:8793`；未引入 tunnel 或公网入口。
- 未读取生产 config/session/runtime/cookie，未复制题库资产，未执行题库写入、删除或修复。
- 未请求、停止或重启 8790/8788。
- 验收结束时 8793 无监听，未保留后台服务。
- 主仓最终 HEAD 仍为 `bc27cba...`，`git status --short` 为空。

## 放行判断

当前实现满足 MVP 的“主线观测分线”定义：关闭观测副作用时，左侧是同 commit 主线；开启观测后，已验证的业务结果、状态、Intent、授权、工具次序和 HTTP 行为不变，唯一新增的是可 fail-open 的 trace、标签和右侧评审界面。
