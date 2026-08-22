# 题库 Agent 输出层升级：阶段 3 纯输出核心

## 结论

阶段 3 已完成一个**不调用模型、尚未接管生产回复**的确定性输出核心。

它现在能接收：

```text
message_key + 精确 protocol + 白名单 facts + allowed_actions + notice_keys
```

并只输出：

```text
固定/模板化用户文案 + 五态协议 + 已验证动作 + 最小公开字段
```

任何未登记、互相矛盾或带污染内容的输入都会 fail closed：只有 exact entry 的 protocol、phase、facts 和动作已经全部验证时，兜底才可沿用该 entry 的动作；前置失败最多保留 protocol 自带主动作。无法证明时统一变为 `SERVICE_UNAVAILABLE`，不回显原始输入。

## 本阶段新增

- `tiku_agent/user_output.py`
  - final 与 progress 两种请求契约；
  - `UserAction` 完整动作枚举；
  - 公开消息和公开联系信息的最小序列化；
  - 43 个首批 final message key；
  - 12 个 progress key；
  - 5 个固定 notice key；
  - 已注册边界 code 与动态 Tool code 的精确协议形状表；
  - facts、动作、媒体交付、PARTIAL 证据、动态题号和 A1 原因枚举校验；
  - 目录启动自检和安全兜底。
- `tests/test_tiku_agent_user_output.py`
  - 覆盖正常目录渲染、协议篡改、未知 key/code、动作矛盾、污染注入、零媒体交付、无结果 PARTIAL、动态题号、A1、notice、progress 与公开 payload。

## 已锁死的关键规则

1. **不接收任意用户文案。** 调用方只能传 key 和白名单事实；A1 也已改成原因枚举到固定模板，模型原句不能进入 final 请求。
2. **动态 Tool code 也必须有精确形状。** 只写成合法大写 code 不代表可信；必须登记其 status、layer、retryable 和主 action。
3. **协议、按钮和话术动作一致。** 主 action、文案提到的动作和 `allowed_actions` 任一矛盾都拒绝。
4. **PARTIAL 必须真有东西可用。** 候选数、题目数或实际送达媒体必须能证明可用结果。
5. **答案文案看实际送达，不看内部路径。** `delivered_image_count=0` 时绝不输出“答案已发出”。
6. **非法模型题号不清洗后放行。** 有稳定 `page_index` 时直接改用“图片第 N 题”；没有合法 index 时 fail closed。
7. **notice 只能来自目录。** 重复项去重，组合顺序固定，不接受自由 note/error。
8. **progress 也是用户输出。** stage 由 progress key 机械派生，不接受任意 message。
9. **普通 JSON 与流式共用同一个公开 payload。** 本阶段提供统一序列化和 stream 外壳；生产入口接线留到后续阶段。
10. **历史 phase 不冒充本次结果。** 例如历史 phase 为 ERROR 时，本次寒暄仍可合法返回 SUCCESS。
11. **动作授权不能借错 key。** code/phase/facts 未完整通过前，该 entry 不能为兜底授权按钮；SUCCESS 也不能携带只属于 PARTIAL 的 retry 动作。
12. **公开 DTO 只能由渲染器创建。** 直接构造或 `replace()` 不能绕过 protocol、kind、ID、stage 和 contact 校验。
13. **章节只公开显示名。** `storage_key` 是实现细节，不允许进入 chapter/source/supported facts。

## 对阶段 2 契约的补齐与安全收紧

实现时确认了几个阶段 2 已表达、但 DTO 示例漏写的字段：

- 增加正整数 `page_index`，专门用于非法 `question_label` 的安全兜底；
- progress 公开消息增加目录派生的 `stage`；
- 搜索尚未建立时，progress 的 `search_id` 可以为空，`request_id` 仍必须有效；
- 目录项显式登记“无动作终止”，目前只用于额度耗尽等合法终止结果；
- `contact_author` 必须同时有授权 fact、允许动作和受限公开 contact 结构。
- 对抗复测证明 A1 任意模型句子无法靠黑名单封闭，因此删除 `bounded_text`，改为 `a1_reason` 枚举选择固定模板。

这些变化没有改变阶段 2 的职责边界；A1 只是把输入形式从“受约束句子”进一步收紧成了“已登记事实”。

## 验证结果

阶段 3 新增对抗测试：

```text
50 tests passed
```

全项目回归：

```text
804 tests passed
```

污染样本覆盖异常名、Windows/Linux 路径、URL/token、Bearer、schema、Traceback，以及它们进入 facts、phase、message key、notice、动作、A1 原因和 progress 的情况。测试检查整个公开序列化结果，而不只检查中文 `text`。

## 还没有完成的事

阶段 3 只证明纯输出核心本身安全，**不能据此宣称生产泄露已经修复**。以下仍在后续阶段：

- A2/A3 各业务分支改成结构化输出请求；
- A3 父流程与子 A2 在渲染前组合事实，停止覆盖/拼接子文本；
- 媒体持久化后再确定 SUCCESS/PARTIAL/ERROR；
- FastAPI、NDJSON 和浏览器停止使用 raw detail/message；
- 公共 session snapshot 删除内部 reason/source/confidence；
- stream/session 层保证 progress sequence 单调、request/search ID 连贯；
- 清理旧的 `render_error(error)`、自由 notice 和原始 ToolResult.error 出口。
- 收紧共享 `RequestProtocol.from_dict`：当前输出核心会二次拒绝 code/status/action 矛盾，但共享反序列化器本身仍需在生产接入前强化。

## 下一阶段

阶段 4 开始生产接入，优先顺序是：

1. 先收紧共享 `RequestProtocol.from_dict`，并做 A2 结构化适配和媒体后定稿；
2. 再做 A3 父子结构化组合；
3. 保留旧入口作为小步回退点，并为每个迁移分支补集成测试。

网页、流式渠道和公共 session payload 的统一收口留到阶段 5；阶段 6 做全链路验收、灰度与旧出口清理。
