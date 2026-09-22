# 章节完整名称优先解析

2026-09-22：修复选择“静定结构位移”却实际检索“静定结构”的问题。

## 原因与范围

生产 Agent 默认流程通过 `resolve_supported_chapter(..., allow_numeric=True)` 解析选章。原实现先匹配兼容简称“静定”，因此“静定结构位移”、`3静定结构位移` 和“改成静定结构位移”都提前返回 `2静定结构`。隔离复现直接检查粗筛收到的章节，确认不只是界面标题显示错误。

解析器现在先使用章节目录识别完整名称、标准存储键和明确方法；只有未识别出明确主题时才回退兼容简称与内部编号。明确不支持的主题不再被兼容规则误收。保留纯数字旧行为，例如 `4` 仍映射 `4力法`；未启用整个严格章节工作流。

CLI 的显式章节参数、旧飞书独立解析器和题库 Skill 的章节键未改变，无需变更协议或重启旧飞书服务。

## 验证

90 项目录、Agent 选章流程和意图回归通过。新增用例覆盖生产默认流程下七个章节的名称、标准存储键、口语选章，以及从“静定结构”改为“静定结构位移”后实际粗筛参数由 `2静定结构` 切换到 `3静定结构位移`。测试使用隔离假工具，不上传用户图片、不调用收费模型。

命令：`$env:PYTHONPATH='tests'; python -B -m unittest test_chapter_catalog test_tiku_agent_chapter_scope_flow test_tiku_agent_intent_v2 test_tiku_agent_intent_v2_blind_contract test_tiku_agent_intent_eval_v2 test_a3_intent_v1 test_evaluate_chapter_scope`
