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

手动数值可省略单位，程序按默认体系补全：`集中 -> kN`、`均布 -> kN/m`、`弯矩 -> kN·m`。

多个荷载用空格一一对应：

```powershell
python scripts/search_by_loads.py loads-search --types "集中" "弯矩" --raws "10kN" "10kN·m" --chapter "2静定结构"
```

按图片检索并启用 LLM 复筛 Top 3：

```powershell
python scripts/search_by_loads.py image-search --image "D:\path\to\question.jpg" --chapter "2静定结构" --rerank
```

实验多 Agent 检索（Qwen 识别分类 + 规则路由主库/字母库 + Zhipu 复筛）：

```powershell
python scripts/multi_agent_search.py --image "D:\path\to\question.jpg" --chapter "2静定结构"
```

图片检索可让 Qwen 根据题干文字自动建议章节。只有高置信且有明确方法文字证据时才会使用自动章节；否则返回 `needs_chapter`，需要手动选择章节：

```powershell
python scripts/multi_agent_search.py --image "D:\path\to\question.jpg" --chapter auto
```

不调用模型、只验证路由和检索：

```powershell
python scripts/multi_agent_search.py --types "均布" --raws "q" --chapter "2静定结构" --no-rerank
```

按上一次检索排名取答案：

```powershell
python scripts/search_by_loads.py answer 1
```

启动桌面软件：

```powershell
python gui.py
```

题库飞书机器人 dry-run 状态机测试：

```powershell
python scripts/feishu_tiku_bot.py dry-run-flow --image "D:\path\to\question.jpg" --chapter 5 --choice 1
```

启动题库飞书机器人和临时公网隧道：

```powershell
.\启动结构力学题库.bat
```

临时飞书事件 URL 会写到 `.tmp_feishu_tiku\feishu_tiku_latest_url.txt`。

## 当前检索流程

- 荷载粗筛逻辑保持原样：默认 Top 5；如果 100% 匹配超过 5 个，则全部进入候选。
- GUI 图片检索默认走多 Agent 流程并启用复筛：Qwen 识别分类，RuleRouter 路由到主库/字母库/复核区，Zhipu 输出复筛 Top 3。
- GUI 手动荷载检索也走 RuleRouter，可按荷载类型路由到主库或字母库；由于没有查询图，手动模式不做 Zhipu 视觉复筛。
- CLI 图片检索需要显式添加 `--rerank` 才启用复筛。
- 实验多 Agent CLI 支持 `--chapter auto`。自动章节只在 Qwen 输出非 `unknown`、置信度不低于 `0.8`、并且章节证据包含明确题干/方法文字时使用；否则返回 `needs_chapter`，不要跨章节搜索。
- 荷载相似度低于阈值的候选不进入复筛；如果进入复筛的候选数不超过 3 个，则跳过 Zhipu，直接输出粗筛结果。
- 复筛只比较候选图和查询图的结构形状。
- 如果复筛后最终相似度 100% 的候选超过 1 个，会额外按杆件长度、跨长、高度和整体比例做打平复核。
- 复筛后的用户可见结果只显示最终相似度。
- `_last_search.json` 会按最终显示排序重写，所以 `answer 1/2/3` 对应复筛后的排名。
- 检索结果路径会自动解析：如果 Excel 里的 `题目名称` 指向的图片不存在或大小写不一致，程序会先在同章节/最近的题目目录下递归查找同名图片；只有候选唯一时才自动更新 live Excel 路径，并删除同路径同荷载的重复行。
- 自动更新 live Excel 前会在项目 `backups/auto_path_repair_时间戳/` 下备份本次进程触碰到的章节 Excel。
- GUI 的结果预览、打开图片、打开答案也走同一套路径解析逻辑，所以图片移动到新子文件夹后，正常检索/点击时可自动恢复。
- 多 Agent 流程已接入 GUI 图片检索：`QwenClassifier` 负责高精度荷载识别和分类，`RuleRouter` 决定查主库、字母库或进入复核区，`Zhipu` 继续负责候选图视觉复筛。
- 实验多 Agent 复筛候选池按库区分阈值：主库非满分候选需 `>=65%`，字母库非满分候选需 `>=50%`；两类库的 `100%` 候选都全部进入候选池，先保召回。若最终复筛候选数不超过 3 个，则不调用 Zhipu，直接返回粗筛结果。
- `scripts/feishu_tiku_bot.py` 是本项目原生的专职题库飞书机器人 MVP。第二大脑项目只作为飞书接入参考，不直接复用其业务代码。当前已支持 dry-run 状态机：收图、问章节、返回 Top 3、选择答案；真实飞书图片下载/上传封装在 `FeishuClient` 中。
- `启动结构力学题库.bat` 会启动 `scripts/start_tiku_bot.ps1`，再由 `scripts/tiku_bot_watchdog.ps1` 保活本地 8788 服务和本项目专属 cloudflared 临时隧道。不要和第二大脑的 8787 小柴服务混用。

## 注意事项

- 不要跨章节自动搜索；章节必须由用户指定、确认，或由 `chapter=auto` 在高置信明确文字证据下自动确定。自动识别失败时必须让用户选择章节。
- 不要直接读写 `config.json`、`config.local.json` 里的密钥；需要结构时看 `config.example.json`。
- 运行前后可用 `python scripts/smoke_test.py` 做只读检查；它会全表检查荷载 JSON、图片路径存在性、路径大小写，并验证旧路径自修复案例。
- 当前主库是数值题和已赋值符号题库。独立字母库位于 live 主库旁边的 `帮做_字母库`，章节 Excel 名称和主库一致，列仍是 `题目名称`、`荷载`。
- 字母库的 `raw` 写入相似度编码，`original_raw` 保留原始字母标注。例如 `2P/a` 写为 `{"type":"均布","raw":"0.021","original_raw":"2P/a"}`；相似度只使用 `type` 和 `raw`，不会读取 `original_raw`。
- 维护字母库/主库拆分时，先运行分类脚本导出 review，再备份 live Excel，最后用应用脚本写回。不要直接按“是否含字母”删除。
- 字母荷载第一版按三个量纲体系归一化，不把字母固定死：均布力主题、集中力主题、集中弯矩主题。长度符号也不固定为 `L`，`a/b/l` 等字母都可作为长度占位；同一个字母可根据荷载类型和表达式形态归入不同体系，例如 `A/长度、A、A*长度` 可表示集中力主题，`A、A*长度、A*长度²` 可表示均布力主题，`A/长度²、A/长度、A` 可表示集中弯矩主题；同体系按倍数映射为保留小数编码后继续走原相似度算法。
- 字母荷载相似度会做同题体系冲突消解：如果同一道题内某个主题体系明显占主导，孤立冲突项仅在相似度内部归入主导体系；不会改写原始 `raw`。
- 如果题干文字或图片说明里给出符号赋值，例如 `P=40kN`、`q=20kN/m`、`F1=40kN`，识别结果的 `raw` 应保留赋值形式；相似度计算时会按等号后的具体数值归一化。不要把这类题只当成纯字母荷载。
