# 章节完整名称优先解析

2026-09-22：修复选择“静定结构位移”却实际检索“静定结构”的问题。

## 原因与范围

生产 Agent 默认流程通过 `resolve_supported_chapter(..., allow_numeric=True)` 解析选章。原实现先匹配兼容简称“静定”，因此“静定结构位移”、`3静定结构位移` 和“改成静定结构位移”都提前返回 `2静定结构`。隔离复现直接检查粗筛收到的章节，确认不只是界面标题显示错误。

解析器现在先使用章节目录识别完整名称、标准存储键和明确方法；只有未识别出明确主题时才回退兼容简称与内部编号。明确不支持的主题不再被兼容规则误收。保留纯数字旧行为，例如 `4` 仍映射 `4力法`；未启用整个严格章节工作流。

CLI 的显式章节参数、旧飞书独立解析器和题库 Skill 的章节键未改变，无需变更协议或重启旧飞书服务。

## 验证

90 项目录、Agent 选章流程和意图回归通过。新增用例覆盖生产默认流程下七个章节的名称、标准存储键、口语选章，以及从“静定结构”改为“静定结构位移”后实际粗筛参数由 `2静定结构` 切换到 `3静定结构位移`。测试使用隔离假工具，不上传用户图片、不调用收费模型。

命令：`$env:PYTHONPATH='tests'; python -B -m unittest test_chapter_catalog test_tiku_agent_chapter_scope_flow test_tiku_agent_intent_v2 test_tiku_agent_intent_v2_blind_contract test_tiku_agent_intent_eval_v2 test_a3_intent_v1 test_evaluate_chapter_scope`

## 发布状态

- 主线修复提交 `d69f1c4f1b9de6cb8fb032372de5889fe554d0fe`。以当前生产 `1350486` 为基线准备候选 `ab1415609961541a4d501c289908ef61d4aa2670`，目录 `F:/ruanjian/lida/b-chapter-selection-20260922`，保留既有费用及 Trace 修复。
- 候选使用生产 Python，以上回归加后台 HTTP、状态路径、启动器和网页客户端测试共 127 项全部通过。
- 维护脚本在 `F:/ruanjian/lida/maint-8790-ab14156`，预定备份在 `F:/cc/_backups/7-题库检索/2026-09-22/chapter-selection-ab14156`。2026-09-22 的 prepare 提权启动被 Windows 返回“操作已被用户取消”，没有生成 prepare 状态文件，没有切换服务。尚未完成生产备份、发布及发布后公网验收；不能称已经上线。
- 后续得到管理员确认后，先运行新维护目录的 `release.ps1 -Mode prepare`；确认状态为 prepared 后再 `-Mode deploy`，最后完成公网生命周期、文字提交及数据保留验证。不要跳过备份或把候选目录当作已运行版本。
