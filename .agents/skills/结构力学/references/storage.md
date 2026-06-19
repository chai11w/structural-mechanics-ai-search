# 储存流程

当用户要把新题存入 Excel 题库时，使用项目脚本处理。

## 图片自动识别并储存

```powershell
python search.py store --image "<图片绝对路径>" --chapter "<章节>"
```

脚本会识别荷载、生成相对路径、追加写入章节 Excel，并按题目路径去重。

## 已知路径和荷载时储存

```powershell
python search.py store --path "<相对题目路径>" --loads '{"loads":[{"type":"集中","raw":"10kN"}]}' --chapter "<章节>"
```

## 荷载 JSON 格式

```json
{"loads": [{"type": "集中", "raw": "10kN"}, {"type": "均布", "raw": "q"}, {"type": "弯矩", "raw": "10kN·m"}]}
```

规则：

- `type` 只能是 `集中`、`均布`、`弯矩`。
- `raw` 保留图中原始标注。
- 没有荷载时使用 `{"loads": []}`，但当前 `search.py store` 在图片未识别到荷载时会取消储存，避免写入空记录。
