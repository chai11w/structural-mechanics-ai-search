# 8790 A2 荷载识别 20 题压力集

这个小型回归集用于检查 8790 最终 A2 识别器是否正确区分集中力、均布荷载和弯矩，尤其是 `ql` 集中力与 `ql²` 弯矩。

- 8 题：`ql` / `qL` / `2ql` / `ql/2` 集中力。
- 4 题：`ql²` / `Pa` 符号弯矩。
- 3 题：其他符号荷载。
- 5 题：数值或混合荷载。
- 覆盖第 2–8 章。

`manifest.json` 只保存 live 题库根下的相对路径，不复制题库图片。预期荷载来自现有 Excel 索引，是“索引支持的暂定真值”；模型结果与索引不一致时，必须再看原图裁决，不能反向直接改库。

验证清单而不调用模型：

```powershell
python scripts/evaluate_load_recognition.py --validate-only
```

关闭缓存运行盲测：

```powershell
python scripts/evaluate_load_recognition.py `
  --max-workers 2 `
  --output experiments/load_recognition_eval_20/results_2026-08-30.json
```

## 2026-08-30 基线

- 模型：`qwen3.7-plus`；缓存关闭。
- 20/20 完成，无请求失败。
- 原始标注匹配、检索等价匹配和荷载类型匹配均为 20/20。
- `ql` 一次幂集中力组 8/8；符号弯矩组 4/4；其他符号组 3/3；数值/混合组 5/5。
- 该基线只证明清晰 live 原题图在本次运行中全部通过；不覆盖拍照旋转、平台压缩或重复运行稳定性。
