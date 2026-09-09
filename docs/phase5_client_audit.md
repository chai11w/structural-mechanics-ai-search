# 阶段五：模型调用与重试核查

范围：标准隔离 A3/A2 入口使用的模型适配器及共用检索 helper。仅假网络与临时数据库，无真实模型调用、题库写入或生产服务操作。

## 可执行证据

`tests.test_execution_client_audit` 实际调用下列适配器，网络出口由 fake `urlopen` 接管；没有替换 `timed_model_call`、执行收据或费用账本。每个适配器分别验证传输超时和带 usage 的响应，共 36 个组合。客户端模型配置、图片和操作身份都在隔离 fixture 中固定。

| 适配器/入口 | 传输超时发出次数 | 收到测试响应后调用次数 | 确认边界 |
| --- | ---: | ---: | --- |
| QwenA3PageObserver.observe | 1 | 2 | 第一次响应格式不合要求，有限 schema 纠错；两次都确认并计费 |
| QwenA3CropVerifier.verify | 1 | 1 | 先确认响应用量，再解析裁图结论 |
| GLM call_glm_json（定位共用传输） | 1 | 1 | 先确认响应，再解析模型 JSON |
| QwenImageTriage.observe | 1 | 1 | 先确认响应，再构造分流观察 |
| QwenTriageReplyClient | 1 | 1 | 先确认响应，再检查说明文字 |
| Qwen / 智谱 ExternalLoadScreen（两入口） | 各 1 | 各 1 | yes/no 解析不能丢掉已确认用量 |
| QwenSafeAnswerClientV0 | 1 | 1 | 按实际响应计费，后续本地策略不抹掉调用 |
| A2 / A3 意图函数（两入口） | 各 1 | 各 1 | 先确认响应，再解析意图 JSON |
| QwenClassifier 的荷载、布局、图像范围、结构类型、尺寸（五入口） | 各 1 | 各 1 | 共用 tracked_qwen_request 或对应带费用记录的适配器 |
| Qwen 图形复筛 | 1 | 1 | 原始响应确认后解析分数 |
| diagram location / verification（两共用 helper） | 各 1 | 各 1 | 共用 tracked_qwen_request |

所有组合都再次提交原操作键：超时保持 UNKNOWN，确认结果只重放原收据，不产生额外网络请求。未知调用不入确认账，但 accounting.pending_runs 必须非零，不能据“未入账”认定免费。确认组合的每条调用使用独立 call_id，按每次 13 tokens 精确核对账本；只有页面理解纠错产生两条、26 tokens。其余每条 13 tokens。

另一个测试用当前安装的真实智谱 SDK 和 `httpx.MockTransport` 注入 ReadTimeout：阶段五配置 `max_retries=0` 后，SDK 确实只发送一次并抛出 APITimeoutError，不仅检查构造参数。

运行：

```powershell
python -B -m unittest -q tests.test_execution_client_audit tests.test_execution_effects tests.test_rerank_prompt tests.test_tiku_agent_tools
```

当前 77 项通过（6.756 秒），其中 client audit 为 3 个参数化/SDK 测试，日志 `.tmp_phase5_1/client_audit_final.txt`。完整阶段以 5.5 最终验收矩阵为准。

## 外层重试与限制

- Qwen HTTP helper 在阶段五关闭传输重试；普通 CLI/飞书作用域保留其原规则。
- `search.py` 的 SDK 创建点显式使用有界重试设置；并发复筛和 `global_search_tool` 均不根据 timeout/failed/unfinished 汇总自动补评。相关测试同时验证旧作用域仍按原策略补评。
- `extract_loads` 只有在 `timed_model_call` 已确认响应后，才允许有限 JSON 纠错；传输层 ValueError 不得伪装成业务 schema 错误。每次纠错单独入账。
- 发送、响应确认、费用补写的故障注入及原键重发在 `tests.test_execution_effects` 中核对；SDK 超时本身不证明供应商未执行。
- 独立 CLI 的批量标注/命令行主函数，以及实验性 decomposer/region mapper 不是标准阶段五运行图的入口。自定义适配器必须声明版本，并把可能计费的请求接入 `timed_model_call`，关闭未知传输自动重试；显式版本声明不能替代这些调用义务。这里不宣称能拦截任意自定义 Python 网络代码。
