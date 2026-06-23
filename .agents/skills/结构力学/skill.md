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

手动数值荷载可省略单位，程序按类型补默认单位：`集中 -> kN`、`均布 -> kN/m`、`弯矩 -> kN·m`。

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

GUI 图片检索默认走多 Agent 路由并启用复筛；荷载相似度低于阈值的候选不进入复筛，若最终复筛候选数不超过 3 个则直接返回粗筛结果。复筛只比较结构形状。若最终相似度 100% 的候选超过 1 个，会额外按杆长/跨长/高度比例打平复核。用户可见结果只显示最终相似度。手动荷载模式也走路由检索，但因为没有查询图片，不做 Zhipu 视觉复筛。
