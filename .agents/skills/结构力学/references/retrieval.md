# 检索流程

## 1. 确定输入和章节

用户可能提供题目图片路径、粘贴图片，或文字描述荷载。章节必须按用户指定；不确定时先确认，不要跨章节自动搜索。

## 2. 执行检索

图片检索并启用复筛：

```powershell
python scripts/search_by_loads.py image-search --image "<图片绝对路径>" --chapter "<章节>" --rerank
```

文字荷载检索：

```powershell
python scripts/search_by_loads.py loads-search --types "集中" --raws "10kN" --chapter "<章节>"
```

数值荷载可省略单位，程序按荷载类型补默认单位：`集中 -> kN`、`均布 -> kN/m`、`弯矩 -> kN·m`。

多个荷载时，`--types` 和 `--raws` 数量必须一致：

```powershell
python scripts/search_by_loads.py loads-search --types "集中" "弯矩" --raws "10kN" "10kN·m" --chapter "<章节>"
```

## 3. 取答案

```powershell
python scripts/search_by_loads.py answer 1
```

图片检索启用复筛后，`answer 1/2/3` 对应复筛后的 Top 3。

## 4. 规则

- 荷载类型只使用 `集中`、`均布`、`弯矩`。
- GUI 图片检索默认启用复筛，不需要额外按钮。
- 荷载相似度低于阈值的候选不进入复筛；如果最终复筛候选数不超过 3 个，直接返回粗筛结果，不调用 Zhipu。
- 复筛只比较结构形状。
- 如果复筛后最终相似度 100% 的候选超过 1 个，会额外比较杆件长度、跨长、高度和整体比例来打平。
- 复筛后的用户可见结果只显示最终相似度。
- 不要绕过项目脚本自己看图判断相似题。
