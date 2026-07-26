# 评审模块 V2 执行报告

## 1. 执行结论

已按 `10_product_spec.md` 的 P0 范围，把默认的“逐事件 JSON 队列”改为“结果优先、按需追因”流程。实现只修改 observation sidecar、其静态前端和 parity tests；没有修改真实 Agent 业务逻辑、题库、搜索/排序逻辑或 `mainline_mirror/source`。

## 2. 改动文件

1. `mainline_mirror/observation/storage.py`
   - 新增服务端白名单 `review_turn` 摘要：只返回输入类型、结果类型、候选/答案数量、章节、状态和是否有题图，不返回用户原文、模型正文、路径或候选内容。
   - `summary()` 改为结果评审口径：已判结果回合、当前可疑节点、未复核节点；不再把未标注事件算作正确或已复核。
   - turn 列表可读取最新结果级标签；保留追加式 revision 和旧事件标签兼容。
2. `mainline_mirror/observation/web.py`
   - turn detail API 同时返回结果卡安全摘要，并只返回当前 turn 的自动异常。
   - 标签读取范围加入 `turn_id`，支持 `target_type=turn` 的结果标签。
   - 注入 UI 改为“本轮结果 → 实际决策链 → 折叠技术详情”；移动端入口默认关闭抽屉。
3. `mainline_mirror/observation/web_static/observer.js`
   - 首屏展示安全输入摘要、最终结果摘要、必要上下文、自动异常数量和四种结果按钮。
   - 正确可一键结束，不创建任何事件标签；明确中间节点保持未复核。
   - 错误/部分正确/无法判断保存后，才显示本回合真实 sequence 因果链。
   - 支持 0/1/多个可疑节点；节点勾选、结论和可选原因使用 `causal_suspicion` revision 记录。
   - 部分正确映射 `uncertain + partial_correct`；无法判断映射 `uncertain + insufficient_evidence`。
   - 错误原因、错误类别和 expected 均可选；expected 不阻塞保存。
   - no_match 三分类保留，并与结果级标签共存；需先选择总体结果，避免仅点 no_match 就被统计成已完成结果评审。
   - 自动异常在结果卡显示数量；有 event_id 的异常在真实因果节点高亮；无 event_id 的异常显示在总览。
   - 原始 JSON 逐事件折叠，默认关闭；保存失败只影响侧栏并给出隐私拒绝提示。
4. `mainline_mirror/observation/web_static/observer.css`
   - 桌面侧栏和 `<=900px` 覆盖抽屉样式。
   - 结果卡优先、异常高亮、单列因果节点；交互控件最小高度 44px。
5. `tests/mainline_parity/test_web_parity.py`
   - 更新旧队列断言为结果优先契约。
   - 新增结果级一键正确、无事件标签、部分正确、多个/未选可疑节点、revision、no_match、expected 可选、隐私拒绝、移动端 CSS 和 JS 语法覆盖。
   - 保留并继续验证主线 DOM/静态资产、cookie 隔离、消息/流式/图片/reset 响应 parity。

## 3. 关键设计取舍

- **安全摘要而非原始答案正文**：当前轨迹刻意不记录用户原文和模型正文。P0 使用服务端白名单语义摘要，无法安全取得的正文明确显示“正文未记录”，没有放宽隐私过滤。
- **未选择即未复核**：只有 turn 结果按钮或可疑节点操作才写标签。正确结果不会批量写中间事件正确标签。
- **可疑节点取消仍可审计**：勾选先写 `uncertain + suspected`；取消写新 revision `correct + dismissed`。统计只把最新状态不是 `dismissed` 的节点算作当前可疑节点。
- **真实链路而非固定步骤**：因果链直接使用 turn detail 的实际事件并按 `sequence` 排序，不补造预期节点。
- **观察失败局部化**：所有新增请求均为 `/api/observation/*`；前端捕获加载/保存失败，未触碰主线响应路径。

## 4. 验证命令与结果

### 最终相关测试

命令：

```powershell
cd F:\cc\7-题库检索\experiments\decision_trace_lab
python -m unittest tests.mainline_parity.test_web_parity tests.mainline_parity.test_agent_parity
```

原始结果摘要：

```text
...............
----------------------------------------------------------------------
Ran 15 tests in 1.871s

OK
```

测试进程同时输出一条既有 `StarletteDeprecationWarning`：`httpx` 与 `starlette.testclient` 的弃用提示，不影响测试结果。

覆盖证据包括：

- 8793 与主线的 `/api/message`、`/api/message/stream`、`/api/image`、`/api/reset` 返回 parity；
- 主线 DOM 经 `strip_observer_markup` 后完全一致，`/assets/demo.css` 和 `/assets/demo.js` 字节一致；
- 外部 `decision_trace_mainline_session` 与旧 `tiku_agent_session` 隔离；
- turn 结果标签、事件可疑标签、revision、未复核口径、no_match 与隐私拒绝；
- Node 可用时对主线 JS + observer JS 执行语法检查，本次通过；
- `<=900px` 抽屉和 44px 最小触达断言。

### Diff 健康检查

命令：

```powershell
git diff --check
```

结果：退出码 0，无空白错误。Git 仅提示工作区 LF 未来可能按配置转换为 CRLF。

### 执行中出现并已修复的失败

- 第一次沿用旧测试时，2 项断言仍要求“人工复核队列”和旧双语 JSON 卡，已更新为 V2 产品契约。
- 新测试初次把仅存在于注入 HTML 的文案错误地断言在 JS 文件中，调整断言作用域后通过。
- 没有遗留失败测试。

## 5. 未决风险与审查重点

1. **安全摘要的信息量有限**：由于现有 observation 轨迹不记录用户原文和最终答复正文，结果卡只能显示“文字答复/候选/答案/no_match”等安全语义摘要。是否足够支持人工判断，需要审查 Agent 结合真实 UI 实测；若未来要展示正文，必须先设计独立的脱敏白名单契约，不能直接开放现有响应。
2. **未做真实浏览器交互截图验收**：本轮有 API、DOM、CSS 契约和 JS 语法自动测试，但没有在 8793 运行实例上做点击链路/移动端截图验证。审查 Agent 应重点验证：四种结果切换、节点多选/取消、保存失败提示、移动抽屉和 no_match。
3. **节点取消采用 `dismissed` revision**：这是为了同时满足追加审计和“当前未选择”。若后续数据消费者只按 dimension 计数而忽略 `error_category=dismissed`，可能误算；本模块新 `summary()` 已正确排除。
4. **旧标签兼容但统计口径已变**：旧 `intent`/`tool_output` 标签仍可读，新的 summary 不再把它们等同于“结果已评审”。这是产品规格要求，但外部脚本若依赖旧 `key_items/reviewed` 字段需要同步迁移。

## 6. 给审查 Agent 的建议验收顺序

1. 先核对产品方向：结果卡是否真正在 JSON/因果链之前，正确是否一键结束。
2. 再检查真实错误流：错误/部分正确/无法判断后选择 0、1、多个节点，未选节点是否没有标签。
3. 检查异常定位、no_match、revision 和隐私拒绝。
4. 最后复跑两组 parity tests，并确认 `mainline_mirror/source` 无改动。
