# 阶段 4.3 A2 采集与验收

4.3 实现 A2 单题的 Checkpoint 自动采集，并提供默认关闭的启动开关。
本阶段的受控验收使用临时目录、真实 Runtime/工具/SQLite Store 和确定性的模型替身；
没有调用外部模型、读写 live 题库、发布或重启 8788/8790/8795。
生产采集仍关闭，正式发布继续遵守固定 release、容量、retention 和备份要求。

## 交付范围

| 批次 | 实现与验证 |
| --- | --- |
| 4.3.1 适配层 | `checkpoint_capture.py` 绑定完整 owner、producer、输入指纹和稳定 failure；支持 standalone 的相等父子 ID |
| 4.3.2 准入 | `checkpoint_capture_gate.py` 默认关闭，要求认证、配额、队列、上传及业务处理均已通过；仅接受真正的 boolean 准入值 |
| 4.3.3 上传与理解 | 上传图创建 workflow `image_accepted`；实际单题分流创建 `image_routed`；章节/结构确定或等待输入时创建 `question_analyzed` |
| 4.3.4 粗筛 | 扫描源提供实际章节/荷载/正分/入池/尺寸过滤数量；尺寸区分 recognized、missing、uncertain、conflict、not_run；候选没有 visible |
| 4.3.5 复筛 | 保存实际策略、成功/失败数和有界分数；先保存全部可见候选，再填充诊断明细；回退前采集逐题结果，保持业务粗筛顺序 |
| 4.3.6 答案与失败 | 绑定候选 ID、rank、generation 和答案 Artifact；无匹配固定为 NO_MATCH、零答案且无 selection；失败只保留稳定 code/kind/fallback |
| 4.3.7 Runtime 与启动 | 普通/筛选线程、预分析/预检入口、章节补充、全局搜索、重试及选答案共用可选 Recorder；已提交的 ID 才写 Trace；跨请求前驱经 Store 授权和审计读取 |
| 4.3.8 受控验收 | 真实 Store 串联、开关、容量拒绝、图片失败、截断、全局搜索、前驱隔离、TTL 和启动校验由下列测试覆盖 |

仅记录实际到达的阶段。粗筛没有候选时不补造复筛；`answer_prepared` 表示准备答案或
明确无匹配，最终交付和评分仍以 Response Store 为权威。Checkpoint 不进入公共响应，
不改变排序、计费、Task State、前端动作授权或执行恢复。

4.4 的 A3 父任务、unit、bbox、裁图和父子 revision 采集不在本次范围。
Runtime 发现 Trace 中存在 A3 `unit_id` 时跳过自动采集，避免把子题错误标成 standalone；
8790/8896 的直接 A2 路径使用本阶段能力，A3 自动采集开关未开放。

## 证据语义

- 上传与答案只从显式受控媒体根读取，拒绝越界、链接及超限内容，不保存路径或原文件名。
- 单条候选明细最多 50 条；分数、数量、截断标志和 visible 均经冻结 V1 验证。
  若业务确实展示超过 50 个候选，保留业务结果，写 `failed / evidence / CAPTURE_INVALID`
  说明该阶段无法表示为 V1 摘要，并使采集健康降级；不伪造可见数量。
- `rerank_policy.threshold` 记录实际复筛入池门槛，`display_all_score` 与 `fallback_limit`
  记录实际展示策略；共享 0.9、8896/8790 的 0.95 均从调用参数取得。
- 无查询图的正常复筛跳过记 `skipped`；模型部分失败回退记 `partial`；全局复筛未完成且
  没有可交付候选记 `failed`。粗筛/答案工具拒绝输入时记失败边界，不虚构正常结果。
- Recorder 使用显式的完整 Git revision；工具提供模型身份与 Prompt 摘要时合入 producer。
  指纹使用图片内容摘要、结构化查询、候选 ID/分数和策略；网络 Trace/Request ID、时间和
  本地文件位置不作为指纹字段。没有结果缓存或执行复用。
- 结构化证据由 Store 以可信时钟保留 30 天；普通图片 3 天，失败图 7 天。
  失败图可增加独立 failed descriptor，但相同内容复用物理 blob；引用不续期。
- 同题新网络请求使用新 Trace；同任务 revision 的最近成功前驱通过有界查询和既有
  read/audit 接口校验。身份、父子版本不同或已过期时不复用。
- 写入、图片读取、序列化、Trace 关联异常均不能阻断搜索；只产生有限安全计数。
  读取和管理继续 fail-closed，容量拒绝和摘要失败会反映到健康状态。

## 开启与回退

未传开关时不安装 Recorder。构造器注入适用于隔离测试；受控运行使用 8790 启动入口的：

```text
--enable-a2-checkpoint-capture
--checkpoint-code-revision <完整40位Git提交>
```

两项必须与 [4.2 runbook](checkpoint_phase4_2_runbook.md) 的七项显式容量、仓库外备份根、
retention 周期及备份保留数一起配置。启用时复核当前 checkout 为该完整提交且干净；
缺配置、提交不一致或工作区有改动均在业务 Runtime 创建前拒绝。
不得在本工作区未提交时强行启用，也不得复用正在运行的服务状态目录做测试。

`build_runtime` 从 8896 builder 传递到 A2 builder；8790 Recorder 仅接受该独立 runtime 的
`a2/` 媒体，复用 4.2 同一个 Checkpoint Store、Trace 容量门和周期 retention controller。
健康状态合并 Store、retention 和 capture 三部分。A2 采集回退只需从下一次受控启动参数中
移除上述开关，已有证据仍由 retention 管理；本次开发没有修改现有计划任务或 watchdog。

## 可重复验证

```powershell
python -B -m unittest -q tests.test_checkpoint_capture tests.test_checkpoint_capture_gate tests.test_a2_checkpoint_stages tests.test_a2_checkpoint_recorder tests.test_a2_runtime_checkpoint_wiring tests.test_a2_checkpoint_integration tests.test_a2_checkpoint_launcher tests.test_checkpoint_contract tests.test_checkpoint_store tests.test_checkpoint_retention tests.test_checkpoint_health
python -B -m unittest discover -s tests -p 'test_*.py'
python -B scripts/run_tiku_agent_8790.py --help
python -B scripts/search_by_loads.py --help
python -B search.py --help
```

`test_a2_checkpoint_integration.py` 使用真实工具扫描、复筛编排、Runtime、Trace 和 Store，
仅替换外部模型及题库数据源。覆盖成功六阶段、无匹配、模型失败前驱、真实容量拒绝、
答案 Artifact 部分失败、默认关闭、同输入跨 Trace 指纹、跨身份/版本拒绝、全局搜索和
复筛部分成功。`test_a2_checkpoint_launcher.py` 验证启用依赖、提交身份与健康接线。
4.2 Store/retention 测试继续验证过期读取拒绝、容量、清理锁、备份及孤儿回收。

CLI 与飞书复用的底层扫描/复筛只新增内部计数和可选观测回调，没有增加采集入口；
项目检索 Skill 的命令和章节/荷载/排名规则不变。
