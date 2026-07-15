# 执行补充交接：incorrect 主按钮单击保存

## 验收失败原因

上一版把 `incorrect（错误）` 主按钮实现成“只展开说明表单”，没有提交 verdict，导致已选
`correct（正确）` 在点击 incorrect 后仍保持绿色。该行为不符合“三个主按钮单击即保存”的
明确规格。

## 修复

- correct、incorrect、uncertain 三个主按钮现在共用同一条单击保存路径。
- 点击 incorrect 会立即请求标签 API；只有成功后才：
  - latest label 从 correct 改为 incorrect；
  - label revision 增加；
  - correct 取消绿色、incorrect 变绿；
  - 卡片按既定规则留在已复核区域。
- 错误说明不再阻塞 verdict：只有已成功保存为 incorrect 的卡片才出现独立的
  `补充说明（可选）` 或 `查看／修改说明` 入口。
- 点击当前已选 incorrect 仍不会生成重复 revision。
- 资源版本提升为 `20260715-review-state-2`，避免继续使用旧脚本缓存。

## 回归验证

- 新增/强化回归契约：前端 incorrect 不再有单独的“仅展开表单”分支，三个按钮都直接调用
  `saveLabel`。
- 后端模拟 exact `correct -> incorrect`：latest label 变为 incorrect，revision 从 1 变为 2；
  随后补充说明再追加 revision 3。
- `python -m unittest discover -s tests\\mainline_parity -v`：12/12 通过。

## 运行说明

现有 8793 若仍是旧进程，需要重启私有 8793 才能加载 Python/HTML 版本更新。不得重启
8790/8788 主线服务。
