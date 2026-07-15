# 执行交接：评审面板复核状态与排序

## 实施范围

按 `17_product_review_panel_addendum.md` 实施，仅修改私有
`decision_trace_lab/mainline_mirror/observation`、对应私有测试；未修改主线仓库。

## 后端权威状态

- turn 详情新增 `latest_labels`，只返回当前 turn 已加载 event 的最新标签。
- 最新标签按 `target_id + dimension` 选择最高 `label_revision`。
- 标签 revision 也按 `target_id + dimension` 递增，仍为 append-only。
- 同 verdict、NO_MATCH 分类和说明完全相同时，返回当前标签并标记 `unchanged`，不追加无意义 revision。
- `expected`、`reason`、`error_category` 可选保存；仍经过原隐私检查。

## 前端行为

- 每张卡片从后端 `latest_labels` 恢复当前 verdict；三个 verdict 按钮中仅当前项为绿色。
- 不显示卡片级“已复核：xx”文字或徽标。
- 队列固定为“待复核”和“已复核”两段，未复核置顶，两段内部都按 trace sequence 升序。
- 顶部计数为 `待复核 X · 已复核 Y · 共 Z 个关键项`。
- 点击 correct/uncertain 直接保存；点击 incorrect 展开可选说明表单，可空表单保存。
- 已填写说明的卡片提供“查看说明”入口。
- 保存期间仅禁用当前卡片控件并显示“正在保存…”。
- 只有 API 成功后才更新绿色按钮和队列位置；首次成功有短暂移动提示。
- 修改标签成功后旧绿取消、新绿生效，卡片继续留在已复核段。
- API 失败不改当前标签、位置、计数或 revision，恢复控件并显示“保存失败，请重试”。
- NO_MATCH 三个分类改为按钮，当前分类同样用唯一绿色表示且可修改。
- 技术详情继续按完整原始 sequence 渲染，不参与人工队列排序。
- CSS/JS 资源版本更新为 `20260715-review-state`。

## 测试

- `python -m unittest discover -s tests\\mainline_parity -v`：12/12 通过。
- `python -m unittest tests.test_lab -v`：15/15 通过。
- 覆盖点包括：
  - latest label 随 turn 详情返回；
  - 重复相同标签不增加 revision；
  - 修改 verdict 增加 revision；
  - incorrect 的三项可选说明持久化并在刷新接口恢复；
  - 选中态、排序、失败提示和禁止额外状态文字的前端契约；
  - 主线与观察镜像业务响应继续保持一致；
  - 主线/观察脚本组合语法检查无全局声明冲突。

全量 `python -m unittest discover -s tests -v` 共 35 项中有 34 项通过，唯一失败是旧
`tests/test_web.py` 仍要求已退役 launcher 包含 `host="127.0.0.1"`；该断言与此前明确退役
旧入口的现状冲突，与本次评审面板改动无关，因此没有越权恢复旧入口。

## 运行说明

需要重启私有 8793 才能完整验收。原因是本次新增了 Python turn-detail 字段和最新标签逻辑，
现有进程不会热加载这些后端改动。请只按私有入口
`python app/run_mainline_observed_web.py` 重启 8793；无需也不得重启主线 8790/8788。
