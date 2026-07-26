# 评审模块 V2 独立验收报告

## 结论

- **PRODUCT PASS**
- **EXECUTION FAIL**

产品方向与用户最新决定一致：先判断最终回答，正确时一键结束；结果不可接受时才按需追因；不要求填写标准答案；允许填写错误原因；不以固定标注数量或逐项清零作为目标。当前执行已覆盖大部分界面与存储流程，但存在 target 未校验和撤销后写成“正确”两个 P0 语义/安全问题，因此执行不能通过。

浏览器视觉验证按协调要求留给主 Agent，本报告只做代码、diff、接口契约与一次自动测试验收。

## PRODUCT PASS 证据

1. `10_product_spec.md` 明确以最终结果卡为入口，`正确`一次保存结束，错误/部分正确/无法判断后才展开真实因果链；这直接落实“先判断最终回答，再反推错误位置”。
2. 产品允许选择 0、1 或多个可疑节点，未选择节点保持未复核，没有固定关键项数量或清零目标。
3. `reason`、`error_category`、`expected`均为可选，且明确禁止把 `expected` 作为错误/部分正确提交条件，符合用户愿写错误原因但不愿填写标准答案的偏好。
4. 产品保留 no_match 三分类、自动异常仅作线索、原始 JSON 折叠、fail-open、隐私白名单、移动端抽屉和主线隔离，范围完整且没有改变真实 Agent 行为。

## EXECUTION FAIL 证据

### P0-1：标签接口没有校验 target，任意或跨会话 target 都可写入

- `mainline_mirror/observation/web.py` 的 `POST /api/observation/labels` 直接把请求 JSON 交给 `store.append_label()`；没有读取当前会话，也没有验证 target 是否属于当前 trace/turn。
- `mainline_mirror/observation/storage.py:103` 的 `append_label()` 只检查 `target_id` 非空、verdict 和 no_match 枚举；不校验 `target_type`、`dimension`，不校验 turn/event 是否真实存在，也不校验 event 与当前 turn/session 的归属。
- 因此客户端可创建任意 ID 的结果标签，或在知道 ID 时修改其他会话/回合的标签。这不满足 target 校验与会话隔离的安全边界；当前测试仅覆盖合法 target，没有覆盖伪造 target、类型不匹配和跨会话 target。

### P0-2：取消可疑节点被写成 `correct`，违反“未选择不等于正确”

- `observer.js` 的节点取消逻辑提交 `dimension=causal_suspicion`、`verdict=correct`、`error_category=dismissed`。
- 这会在追加式标签中留下一个当前 `correct` verdict。虽然新 `summary()` 用 `dismissed` 排除了可疑节点计数，UI 也取消勾选，但标签 API 的当前记录仍把该节点表达为正确。
- 这与产品规格“未选择的中间事件保持未复核，不能自动算正确”和验收标准“未选择事件在 API、统计和 UI 中均为未复核”冲突。执行报告将其称为可审计撤销，但审计记录不应借用 `correct` 表达撤销。
- 当前测试反而固化了该行为：先选中，再提交 `correct + dismissed`，只断言 `suspicious_nodes == 0`，没有断言 API 当前状态仍为未复核且不计入正确/已复核。

## 其余重点项核对

- **结果优先 / 一键正确：通过。** 首屏为结果卡；正确按钮直接写 turn 级 `result_interpretation`，不会批量创建事件标签，并显示中间节点保持未复核。
- **0/1/多个可疑节点：基本通过。** 非正确结果保存后显示按真实 sequence 排序的事件链，可不选、单选或多选；没有固定数量约束。撤销语义需按 P0-2 修复。
- **数量/逐项导向：通过。** 旧“待复核/已复核/共 N 个关键项”已移除；自动异常数量属于诊断提示，并明确“不代表错误”。
- **no_match：通过。** 三分类仍绑定 turn 结果标签，且 expected 不必填写；需先判总体结果，避免只点 no_match 就算完成评审。
- **自动异常：通过静态契约。** 结果卡显示异常数；有 event_id 的异常高亮到对应节点，无 event_id 的异常显示总览；0 异常明确不等于正确。
- **隐私：基本通过。** 结果卡使用服务端白名单摘要；原因/期望字段继续经过绝对路径、禁止键和超长内容检查；未放开原始正文。target 校验缺失仍是独立的写入安全问题。
- **移动端：自动契约通过，视觉待主 Agent。** `<=900px` 使用覆盖抽屉，`<=420px` 控件单列，操作控件最小高度 44px；真实浏览器点击、窄屏信息层级和遮挡由主 Agent 验证。
- **主线隔离 / fail-open：通过现有测试。** 主线 DOM 和 `/assets/demo.*` parity、cookie 翻译、消息/流式/图片/reset 响应保持一致，`mainline_mirror/source` 当前无 diff。

## 唯一一次测试

命令：

```powershell
python -B -m unittest tests.mainline_parity.test_web_parity tests.mainline_parity.test_agent_parity
```

结果：15 项通过，`Ran 15 tests in 1.731s`，`OK`。另有既有 `StarletteDeprecationWarning`，不影响本次结论。测试通过不能覆盖上述两个缺口，因为现有用例未测试非法/跨会话 target，并明确接受了 `correct + dismissed` 的撤销表示。

## 最小修复清单（退回执行 Agent）

1. 在 label API 服务端基于当前隔离会话校验 target：
   - `target_type=turn` 只能指向当前 trace 中真实存在的 turn，且结果维度只能用于该 turn；
   - `target_type=event` 只能指向当前 trace 中真实存在的 event，并校验其所属 turn；
   - 拒绝未知 `target_type`、未知/不兼容 `dimension`、伪造 ID、类型错配和跨会话 target；返回 400/404，且不得写标签。
2. 修正取消选择语义：不要用 `verdict=correct` 表达撤销。采用明确的 deselected/tombstone 审计状态，或等价的追加式撤销记录；对 UI、summary 和评审 API 的当前派生状态必须统一呈现为 `unreviewed`，不得计入正确、错误或已复核。
3. 增加最小回归测试：非法 target、turn/event 类型错配、跨会话 target 均被拒绝且 labels 文件不增长；选中后取消时保留 revision 审计，但当前评审状态为未复核，任何正确/已复核统计均不增加。

修完以上两项即可进入下一轮执行复核；无需重做产品规格。
