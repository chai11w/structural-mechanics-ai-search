# 评审模块 V2 执行返工报告（Revision 1）

## 1. 本轮范围

只修复 `30_review_report.md` 指出的两个 P0 阻塞项：

1. 标签 API 的真实 target、类型/维度兼容性和当前隔离会话归属校验；
2. 可疑节点取消选择的明确撤销/tombstone 语义。

没有修改产品规格、真实 Agent 逻辑或 `mainline_mirror/source`。

## 2. P0-1：标签 target 与会话校验

改动文件：`mainline_mirror/observation/web.py`

新增 `_validate_label_target()`，`POST /api/observation/labels` 在写入前：

- 从已由 middleware 翻译后的内部 session cookie 取得当前隔离会话；
- 只读取该会话 `trace_key(session_id)` 下的真实事件；
- `target_type=turn` 只接受当前 trace 中真实 turn，且只允许 `result_interpretation`；
- `target_type=event` 只接受当前 trace 中真实 event，并按真实 event_type 校验：
  - `intent` 仅用于 `intent_decided`；
  - `tool_output` 仅用于 `tool_completed`；
  - legacy `result_interpretation` 仅用于 `turn_completed`；
  - `causal_suspicion` 可用于真实因果链事件；
- 未知 target_type、类型错配、维度错配返回 400；
- 伪造 ID、无会话和跨会话 target 返回 404，避免泄露其他会话目标是否存在；
- 所有拒绝均发生在 `append_label()` 前，不增加 labels 文件行数。

## 3. P0-2：明确 tombstone 撤销语义

改动文件：

- `mainline_mirror/observation/storage.py`
- `mainline_mirror/observation/web_static/observer.js`

行为变更：

- 勾选可疑节点仍追加 active `causal_suspicion` 标签；
- 取消勾选改为提交 `label_state=withdrawn`，不再提交 `verdict=correct + error_category=dismissed`；
- storage 追加一条只含 target、dimension、`label_state=withdrawn`、revision 和时间的 tombstone；tombstone 没有 verdict；
- `latest_labels()` 在计算每个 target+dimension 最新 revision 后过滤 tombstone，因此 turn detail 的 current/latest API 把该节点呈现为未复核；
- `summary()` 基于过滤后的 current labels，因此撤销节点不计入可疑、正确、错误或已复核，并回到 `unreviewed_nodes`；
- `labels.jsonl` 仍保留 active revision 和 withdrawn revision，审计链完整；
- 为兼容返工前可能产生的旧 `correct+dismissed` 记录，`latest_labels()` 也把这种旧记录按未复核处理；新前端不再写这种表示。

## 4. 回归测试

改动文件：`tests/mainline_parity/test_web_parity.py`

新增/加强覆盖：

- 伪造 event target 返回 404，labels 行数不增长；
- event ID 冒充 turn、turn ID 冒充 event 返回 400，labels 行数不增长；
- event 类型与 dimension 不兼容返回 400，labels 行数不增长；
- 未知 target_type 返回 400，labels 行数不增长；
- 第二个隔离客户端尝试标注第一个会话的 event 返回 404，labels 行数不增长；
- 可疑节点 active revision 后取消，产生 revision 2 tombstone；
- tombstone 不含 verdict；
- turn detail 的 `latest_labels` 不再返回已撤销节点；
- 原始 `labels()` 仍保留 revision 1 与 revision 2 审计记录；
- summary 的 `suspicious_nodes=0`、`unreviewed_nodes>0`，且 result verdict 统计不增加 `correct`。

## 5. 验证命令与原始结果

命令：

```powershell
cd F:\cc\7-题库检索\experiments\decision_trace_lab
python -B -m unittest tests.mainline_parity.test_web_parity tests.mainline_parity.test_agent_parity
git diff --check
```

结果：

```text
................
----------------------------------------------------------------------
Ran 16 tests in 1.944s

OK
```

`git diff --check` 退出码 0；只有既有 LF/CRLF 提示。测试仍输出既有 `StarletteDeprecationWarning`，不影响结果。

## 6. 剩余风险

1. target 校验每次写标签会读取当前 trace 的 JSONL 事件；当前个人评审规模可接受，若轨迹量显著增长需要索引，但不影响本轮正确性。
2. `ObservationStore.append_label()` 仍是底层追加原语，不具备 session 上下文；安全归属校验位于唯一公开写入口 `/api/observation/labels`。新增公开写入口时必须复用 `_validate_label_target()`。
3. 旧 `correct+dismissed` 记录在 raw 审计文件中仍保留，但 current/latest API 与 summary 已统一按未复核处理；新代码只写明确 tombstone。
