# 8890 复杂题图评测集

当前状态：`DRAFT`。

本目录只维护评测元数据和人工标签，不提交用户反馈原图。题库标准图通过相对路径引用外部 live 题库；反馈案例只记录稳定的案例哈希、媒体短哈希和人工复核状态。任何运行态绝对路径、用户原文、邀请码、Prompt 和内部推理都不能进入版本库。

## 采样规则

- 标准单题以现有题库图为 A2 基线，先覆盖梁、钢架、桁架和不同章节；这些标签在图片人工复核前只能标为 `provisional`。
- 真实反馈图优先保留用户首次上传的原图，候选图和答案图只用于人工理解，不作为输入样本提交。
- 同一图片内容只保留一个样本；反馈案例中的重复上传记录为 `duplicate_of`。
- A1 必须有白墙、风景、人物、无关截图或不可恢复模糊图；不能用“看起来不相关”的结构题冒充 A1。
- A3 必须覆盖多题、一题多图、公共题干、单位力图、内力图、绑定不确定和需要用户确认等情况。

## 人工标签

每个样本最终需要填写：

- `expected_route`: `A1`、`A2` 或 `A3`；
- `question_count`、`subquestion_count`、`diagram_count`；
- 每个图的角色：`original_structure`、`auxiliary_unit_load`、`internal_force_diagram`、`deformation_diagram`、`dimension_or_annotation`、`irrelevant` 或 `unknown`；
- 题目/子题与图的绑定；
- 实际外荷载，只允许来自 `original_structure`；
- 图片质量、截断、歧义和人工复核人/日期。

## 当前缺口

详见 `manifest.json` 的 `coverage`。在补齐 A1/A3 样本和复核反馈图片前，不启动视觉模型评测，也不把本清单当作生产验收集。
