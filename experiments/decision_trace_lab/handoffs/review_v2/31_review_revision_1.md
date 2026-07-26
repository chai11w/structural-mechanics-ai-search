# 评审模块 V2 独立复验报告（Revision 1）

## 结论

- **PRODUCT PASS**
- **EXECUTION PASS**

本轮只复验 `30_review_report.md` 的两个 P0 阻塞项。两项均已修复，且新增测试覆盖了此前缺失的拒绝路径与撤销后的未复核语义；未发现新的代码级阻塞。浏览器视觉验收仍由主 Agent 完成。

## PRODUCT PASS

产品规格未改动，仍准确落实用户最新决定：先判断最终回答；正确时一键结束；结果不可接受时再按需追因；可选 0、1 或多个真实问题节点；不要求填写标准答案；错误原因可选；不以逐项复核或固定标注数量为目标。

本轮返工没有扩大产品范围，也没有用实现便利改变“未选择即未复核”的产品定义。

## EXECUTION PASS

### P0-1：target 与会话校验——通过

证据：

1. `mainline_mirror/observation/web.py:109-118` 的唯一公开标签写入口在 `append_label()` 前读取当前隔离会话，并调用 `_validate_label_target()`；校验失败不会落盘。
2. `mainline_mirror/observation/web.py:203-244` 只允许：
   - 当前 trace 中真实 turn 使用 turn 级 `result_interpretation`；
   - 当前 trace 中真实 event 使用与其 `event_type` 兼容的事件维度；
   - 未知 target type、turn/event 类型冒充、维度错配被拒绝；
   - 无会话、伪造 ID 和跨会话 target 返回 404。
3. 新增测试 `test_label_api_rejects_forged_mismatched_and_cross_session_targets_without_writes` 覆盖伪造 target、类型错配、维度错配、未知类型和跨会话写入；每次拒绝后均断言 labels 行数不增长。
4. `mainline_mirror/source` 当前无 diff，校验没有侵入真实 Agent 主线。

结论：上一轮“任意或跨会话 target 可写入”的阻塞已关闭。

### P0-2：撤销后的未复核语义——通过

证据：

1. `observer.js:322-330` 在取消勾选时提交 `label_state=withdrawn`，不再提交 `verdict=correct + error_category=dismissed`。
2. `storage.py:116-134` 追加无 verdict 的 withdrawn tombstone，保留 revision 审计链；不存在旧标签时禁止凭空撤销。
3. `storage.py:165-178` 先按 target+dimension 选择最新 revision，再从 current/latest 结果中过滤 withdrawn tombstone；兼容过滤旧 `correct+dismissed` 记录。
4. 新增测试验证：active revision 1 后产生 withdrawn revision 2；tombstone 不含 verdict；turn detail 的 `latest_labels` 不再返回该节点；raw audit 仍保留两条 revision；`suspicious_nodes=0`、`unreviewed_nodes>0`，结果 verdict 统计不增加 `correct`。
5. 前端 `putLabel()` 收到 withdrawn 后删除 current label，因此 UI 当前状态与 API、summary 一致回到未复核。

结论：撤销现在是可审计 tombstone，不再借用“正确”表达；满足“未选择不等于正确”。

## 唯一一次测试

命令：

```powershell
python -B -m unittest tests.mainline_parity.test_web_parity tests.mainline_parity.test_agent_parity
```

结果：

```text
................
----------------------------------------------------------------------
Ran 16 tests in 1.858s

OK
```

另有既有 `StarletteDeprecationWarning`，不影响本轮结论。

## 剩余非阻塞风险

1. target 校验每次读取当前 trace JSONL；个人评审规模下不影响正确性，数据量增长后可再做索引优化。
2. `ObservationStore.append_label()` 是无会话上下文的底层追加原语；当前唯一公开写入口已强制校验。未来如增加其他公开写入口，必须复用同一校验。
3. 移动端抽屉、真实点击链路、窄屏遮挡及视觉层级未在本轮验证，按任务约定由主 Agent 做浏览器验收。

本轮无需再退回产品或执行 Agent；可由主 Agent完成浏览器视觉与最终独立验证后交付。
