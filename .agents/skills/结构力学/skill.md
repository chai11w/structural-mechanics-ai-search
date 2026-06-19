---
name: tiku-search
description: "检索结构力学题库：按图片或荷载搜相似题目，并按排名取答案"
user-invocable: true
---

# 结构力学题库检索

按荷载检索：

```powershell
python scripts/search_by_loads.py loads-search --types "均布" --raws "20kN/m" --chapter "2静定结构"
```

按图片检索并启用 LLM 复筛：

```powershell
python scripts/search_by_loads.py image-search --image "D:\path\to\question.jpg" --chapter "2静定结构" --rerank
```

取答案：

```powershell
python scripts/search_by_loads.py answer 1
```

桌面软件：

```powershell
python gui.py
```

GUI 图片检索默认输出复筛后的 Top 3；手动荷载模式保持原荷载粗筛逻辑。
