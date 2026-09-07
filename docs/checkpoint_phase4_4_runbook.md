# 阶段 4.4 A3 父子采集与验收

4.4 在冻结的 V1 契约和 4.2/4.3 控制面上接入 A3 父页面、unit 裁图及子 A2 证据。
采集默认关闭。开发和验收使用临时目录、真实 Runtime/SQLite Store、确定性的模型替身；
不调用外部模型，不读取 live 题库，不部署或重启现有服务。

## 交付范围

| 批次 | 内容 |
| --- | --- |
| 4.4.1 父阶段投影 | 上传、实际路由、整页理解、裁图几何/grounding、六项校验和独立外荷载门禁 |
| 4.4.2 Runtime 采集 | 单题自动下行、多题并发校验、人工裁剪及回退、理解重试和稳定失败边界 |
| 4.4.3 子 A2 关联 | 请求内可信父身份经 ContextVar 传入 A2 线程；父 revision 与子 revision 分别绑定 |
| 4.4.4 生命周期与启动 | 复用 Store、七项容量、周期 retention、健康降级和完整提交门禁；父子使用同一 Recorder |
| 4.4.5 隔离验收 | 真实父子链、Artifact 去重、权限/版本隔离、并发、失败、截断和兼容回归 |

## 证据语义

- 仅保存实际到达的阶段。无可检索 unit 时保存整页 `no_match`；不补造裁图或子 A2。
- 整页摘要保留真实总数，最多保存 50 个 unit，11～50 个完整列出；更多时显式标记
  `stored_unit_count/units_truncated`。文本摘录单项不超过 1000 字，并进一步缩短以满足
  单条结果 64 KiB 上限；unit 身份和计数保持不变。不保存模型原文、notes 或全文 OCR。
- 自动裁剪保留原始 bbox、扩展 bbox、实际像素范围及尺寸；人工裁剪记录真实像素范围，
  不伪造模型 bbox。校验保留固定六项检查和 `not_run/not_configured/yes/no/error` 外荷载状态。
- 并发校验每完成一个 unit 即采集其结果；前驱按 unit 查找，不将另一题的裁图串入本题。
  校验拒绝保持 `needs_input`，模型/写图失败只保存稳定 code，不保存异常正文。
- 父页面源图和每个 unit 裁图由 Store 去重；准备、校验及子 A2 引用兼容的 Artifact。
  普通图片 3 天、失败图片 7 天，重复引用不续期；不同 owner/retention descriptor 可以共享
  同一物理 blob。子题失败的 `source_page` 始终指向父页面，不把裁图误标成原图。
- 读取父证据先执行 owner、版本和查看审计；裁图引用另经 Artifact 的 TTL/权限检查。
  过期、审计失败或缺失时放弃该引用并报告降级，业务可继续。
- 子 A2 使用实际 `workflow_search_id/workflow_task_revision/unit_id`，候选 generation
  按子 A2 `task_revision` 校验。跨请求优先查同一子任务的成功前驱，再受控关联父 unit。
  请求退出即清除绑定；线程传播使用现有 `submit_with_trace_context`。
- 路由直接进入 A2 时没有 A3 unit，继续使用 4.3 的 standalone 身份。入口的页面路由
  证据和 standalone A2 通过 Trace 定位，同图共用物理 blob；不伪造 unit 或修改 V1 拓扑。
- 证据写入失败不改变章节、候选排序、费用、公共响应或 Task State；成功提交后才写 Trace
  的 `checkpoint_id`。图片复制失败保留已形成的结构化结果并降级；缺 Trace、未认证、
  容量拒绝等均不能触发业务重放。health/login/media GET、陈旧动作和准入拒绝不创建新阶段。
- 父阶段 producer 记录完整代码 revision；实际模型对象可提供模型身份和 Prompt 摘要。
  指纹排除 Trace/Request ID、时间及路径。Checkpoint 不提供执行恢复或动作授权。

## 开启与回退

8790 启动入口新增：

```text
--enable-a2-checkpoint-capture
--enable-a3-checkpoint-capture
--checkpoint-code-revision <完整40位Git提交>
```

A3 开关要求同时启用 A2；缺少 4.2 七项显式容量、retention 周期、仓库外备份根、备份保留数，
或 checkout 非指定的干净完整提交，均在创建业务 Runtime 前拒绝。
媒体读取限定该 runtime 的 `a2/` 与 `a3_sessions/`，拒绝越界、链接和超限图片。
容量与部署约束见 [4.2 runbook](checkpoint_phase4_2_runbook.md)。

8790 通过 8896 builder 将同一个 Recorder 传给 A3 和子 A2；两者共用一个 Store 和
retention controller，健康状态包含两者的安全降级计数。回退时在下一次受控启动中移除
A3 开关即可；只保留 A2 开关时恢复 4.3 的采集范围，已有证据继续由 retention 管理。
本次不修改计划任务、watchdog、现有服务状态目录或飞书配置。

## 可重复验证

```powershell
python -B -m unittest -q tests.test_a3_checkpoint_integration tests.test_a3_checkpoint_launcher tests.test_a3_runtime tests.test_a2_checkpoint_integration tests.test_checkpoint_store tests.test_checkpoint_retention
python -B -m unittest discover -s tests -p 'test_*.py'
python -B scripts/run_tiku_agent_8790.py --help
python -B scripts/search_by_loads.py --help
python -B search.py --help
```

新增测试覆盖单题自动下行到答案、多题并发和不同会话隔离、人工裁剪及校验失败、不同父子
revision、跨身份/版本/题目拒绝、陈旧动作、缺 Trace、默认关闭、真实容量拒绝、图片复制
失败、过期图片不复用或续期、长页面摘要及跨 Trace 指纹。已有 A2/A3 业务测试和 Store
生命周期测试继续作为兼容基线。

2026-09-07 验证：新增 A3 采集/启动测试 23 项、Checkpoint/Store/retention 联合 79 项通过；
最终全仓 1383 项通过（60.862 秒）。三个 CLI 帮助命令及 `git diff --check` 通过。

4.5 的诊断查询入口及阶段整体诊断验收仍单独推进；本阶段不新增前端或诊断 CLI 功能。
