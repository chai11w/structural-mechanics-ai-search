# 阶段 6：全链路验收与旧出口清理

## 结论

阶段 6 已完成仓库实现和回归验收。A2、A3、普通 HTTP、NDJSON Stream、浏览器和公共 session 的用户输出都经过同一结构化边界；没有结构化草稿、协议矛盾或公开载荷不合法时，一律返回固定安全失败，不再回退到旧文本。

本阶段没有新增模型调用，也没有重启或部署现有服务。

## 已清理的旧出口

- `_agent_payload` 不再读取 `AgentResponse.text` 作为公开回复；`output=None` 直接 fail closed，并尽量保留合法的 request/search ID。
- API 异常按 path、状态码和登记协议选择固定公共消息；`HTTPException.detail` 仅用于受限的错误分类，不参与公共文案，也不会与 `str(exc)` 或未知 JSON body 一起发给用户。
- Stream 只发 `{ "type": "progress|result|error", "data": PublicMessageV1 }`，不再有顶层 `message`、`detail` 或任意 stage 文案。
- progress 文案来自登记目录，同一流中 request ID 固定、sequence 从 1 单调递增；已有 search ID 时最终结果必须保持一致。
- 浏览器只读取规范消息的 `text` 和 `allowed_actions`；网络、超时、HTTP 和客户端协议错误使用本地固定目录。
- 公共 session 只保留 UI 必需字段。内部 intent reasoning、reason codes、裁剪诊断和路径不公开；题目标题与上下文经过限长、控制字符和敏感内容过滤。

## 一并修复的契约问题

- A3 子流程结果重新绑定父请求 ID，父子结果只按结构化事实组合。
- A3 缺少当前题绑定时不再退回裸 A2 候选或答案，而是安全失败。
- `UNKNOWN_CHAPTER` 使用与 tool/NEEDS_INPUT 协议匹配的固定可行动提示。
- `LOAD_ROUTE_NEEDS_REVIEW` 补齐精确协议注册，schema-v1 可以严格往返。
- 候选图片必须整批交付；答案按实际交付数量输出，零张不能宣称成功。

## 验证

```text
阶段 4-6 专项：180 tests OK
全仓回归：864 tests OK
JavaScript：node --check 通过
差异检查：git diff --check 通过
```

专项和全仓测试包含固定模板、协议篡改、污染文本、父子题号绑定、媒体部分/零交付、HTTP 非反射错误、Stream ID/序号、公共 session 白名单以及浏览器只消费规范消息等场景。

## 后续边界

- 偏门、低风险解释的模型润色仍未实现；以后接入时只能读取审核后的结构化事实，并且失败时回到当前固定模板。
- 8788 飞书、8795 管理后台和 CLI 不在本轮改动范围内。
- 本阶段完成的是代码与测试闭环；部署必须另行按端口隔离规则执行。
