# 阶段 5：浏览器输出边界

## 目标

浏览器只展示已通过公共输出契约的服务端文案和动作。HTTP 错误详情、流式事件中的非规范字段、浏览器异常原文都不能直接显示给用户。

## 已完成

- `demo.js` 的流式读取只接受：

  ```json
  {"type":"progress|result|error","data":{...PublicMessageV1}}
  ```

- `progress` 使用 `data.text` 更新处理中状态；`result` 和 `error` 都先校验 `data` 是否是规范公共消息。
- 最终聊天消息的 `text`、`allowed_actions` 和恢复按钮只来自通过校验的 `PublicMessageV1`。候选题按钮也要求该消息允许 `select_candidate`。
- 未知 HTTP body、未知流式事件、JSON 解码失败和协议形状不正确时，统一使用浏览器本地的固定 `RESPONSE_INVALID` 文案和重试动作；不读取或展示 `detail`、`message`、异常字符串。
- 网络中断、请求超时、常见 HTTP 状态和本地图片校验使用固定 code/text/action 目录；本地图片校验直接返回结构化 code，不从提示文字反推类型。
- `/api/session` 仍兼容现有 `{ "uploaded_image": "...", "session": { ... } }` 外层形状，但浏览器只接收最小会话字段：会话有效性、阶段、活动图片、任务版本、候选代次/数量、搜索 ID 和已审核的 A3 快照。
- 已更新浏览器脚本缓存版本号，避免继续加载旧的前端实现。
- FastAPI 的普通错误只返回规范 `PublicMessageV1`，不再序列化 `HTTPException.detail` 或异常字符串。
- Stream 的 progress/result/error 全部使用 `{ "type": "...", "data": PublicMessageV1 }`；progress 使用同一请求 ID 和单调序号，已有搜索链路时保持搜索 ID 一致。
- 回复和 `/api/session` 的会话状态统一经过服务端白名单；题目标题和题干上下文只在限长并过滤控制字符、路径、密钥特征和内部诊断词后公开。

## 浏览器期望的公共消息

`PublicMessageV1` 至少包含：

```json
{
  "schema_version": 1,
  "kind": "result",
  "message_key": "search.candidates.ready",
  "text": "...",
  "allowed_actions": ["select_candidate"],
  "request_id": "req_...",
  "search_id": "search_...",
  "status": "SUCCESS",
  "layer": "tool",
  "code": "RERANK_COMPLETED",
  "retryable": false,
  "action": ""
}
```

流式进度消息使用同一公共消息形状，`kind` 为 `progress`，并额外携带 `sequence` 和 `stage`。媒体 URL、会话快照和 A3 UI 所需状态仍是受限的附加字段；它们不参与用户文案的决定。

## 验证

- 新增 `tests/test_tiku_agent_web_output_contract.py`，锁定规范流式读取、HTTP/detail 不直通、最终文案/动作来源、会话最小快照和本地图片校验 code。
- `node --check tiku_agent/demo_web/demo.js` 已通过。
- `git diff --check` 已通过。
- 输出层阶段 4-6 专项 `180` 项测试通过。
- 全仓 `864` 项测试通过。

## 未做事项

- 本阶段没有接入模型润色。
- 本阶段没有重启或部署 8790、8788、8795。
- 8788 飞书、8795 管理后台和 CLI 不在本轮输出层接入范围内。
