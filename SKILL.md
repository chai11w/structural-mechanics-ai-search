---
name: tiku-search
description: "检索结构力学题库：按图片或荷载搜相似题目，并按排名取答案"
user-invocable: true
---

# 结构力学题库检索

这个项目用于维护和检索结构力学题库。题库索引保存在章节 Excel 中，第一列是题目图片相对路径，第二列是荷载 JSON。

## 常用命令

按荷载描述检索：

```powershell
python scripts/search_by_loads.py loads-search --types "均布" --raws "20kN/m" --chapter "2静定结构"
```

多个荷载用空格一一对应：

```powershell
python scripts/search_by_loads.py loads-search --types "集中" "弯矩" --raws "10kN" "10kN·m" --chapter "2静定结构"
```

按图片检索并启用 LLM 复筛 Top 3：

```powershell
python scripts/search_by_loads.py image-search --image "D:\path\to\question.jpg" --chapter "2静定结构" --rerank
```

按上一次检索排名取答案：

```powershell
python scripts/search_by_loads.py answer 1
```

启动桌面软件：

```powershell
python gui.py
```

## 当前检索流程

- 荷载粗筛逻辑保持原样：默认 Top 5；如果 100% 匹配超过 5 个，则全部进入候选。
- GUI 图片检索默认启用复筛，不需要额外按钮，最终只显示复筛后的 Top 3。
- CLI 图片检索需要显式添加 `--rerank` 才启用复筛。
- 荷载相似度低于 50% 的候选不进入复筛；如果过滤后不足 3 个，则只输出过滤后的复筛结果。
- 复筛只比较候选图和查询图的结构形状。
- 如果复筛后最终相似度 100% 的候选超过 1 个，会额外按杆件长度、跨长、高度和整体比例做打平复核。
- 复筛后的用户可见结果只显示最终相似度。
- `_last_search.json` 会按最终显示排序重写，所以 `answer 1/2/3` 对应复筛后的排名。

## 注意事项

- 不要跨章节自动搜索；章节必须由用户指定或确认。
- 不要直接读写 `config.json`、`config.local.json` 里的密钥；需要结构时看 `config.example.json`。
- 运行前后可用 `python scripts/smoke_test.py` 做只读检查。
- 字母荷载第一版按体系归一化：`q/qL/qL²` 等为 q 体系，`F/P/Fp` 及 `F/L、FL、P/L、PL、Pa` 等为力体系，`M/L²、M/L、M` 为弯矩体系；同体系按倍数映射为保留小数编码后继续走原相似度算法。
