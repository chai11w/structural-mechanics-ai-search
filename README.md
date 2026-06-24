# 结构力学题库 AI 检索系统

这是一个用于结构力学答疑题库的本地检索工具。目标很直接：把题目图片或荷载描述输入进去，快速找到题库里最相似的题目，并按排名打开对应答案。

目前支持桌面 GUI、命令行检索和飞书机器人三种入口。题库按章节 Excel 维护，检索时会先识别荷载，再在对应章节内做相似题匹配。

## 主要能力

- 图片检索：上传题图后，用 DashScope 的 Qwen 识别荷载和可见题干信息。
- 手动荷载检索：可直接输入 `集中/均布/弯矩` 和荷载值，数值题可省略常用单位。
- 自动章节识别：题图里出现明确方法文字时，自动识别章节；证据不足时不乱猜，要求手动选章节。
- 双题库检索：数值荷载主库和字母荷载库分开维护，字母题用映射编码复用原相似度算法。
- 视觉复筛：粗筛候选后，可用 Zhipu 视觉模型做 Top 候选复筛。
- 飞书移动端：手机在飞书发题图，机器人返回相似题 Top 3，可继续选择答案。

## 题库结构

当前按 5 个章节维护：

- `2静定结构`
- `3静定结构位移`
- `4力法`
- `5位移法`
- `6力矩分配`

主库保存数值荷载题和已赋值字母题，例如 `P=40kN`、`q=20kN/m`。  
字母库保存未赋值字母题，例如 `q`、`2P/a`、`M`，并写入特殊小数编码用于相似度计算。

## 快速使用

启动桌面端：

```powershell
python gui.py
```

启动飞书题库机器人和临时公网隧道：

```powershell
.\启动结构力学题库.bat
```

临时飞书事件 URL 会写入：

```text
.tmp_feishu_tiku\feishu_tiku_latest_url.txt
```

图片检索：

```powershell
python scripts/multi_agent_search.py --image "D:\path\to\question.jpg" --chapter auto
```

手动荷载检索：

```powershell
python scripts/search_by_loads.py loads-search --types "均布" --raws "20" --chapter "2静定结构"
```

获取上一次检索的第 1 个答案：

```powershell
python scripts/search_by_loads.py answer 1
```

## GUI 和飞书逻辑

GUI 的章节下拉框默认是 `自动识别章节`。如果图片中能识别到明确章节证据，就自动检索；如果证据不足，会提示手动选择章节，不会跨章节乱搜。

飞书机器人默认也是自动章节模式：

- 发送图片后，机器人会先给图片加一个 `OK` 表情，表示正在处理。
- 识别成功后返回相似题 Top 3。
- 回复 `1/2/3` 获取对应答案。
- 回复 `0` 结束本次检索。
- 回复 `a` 在自动章节和手动章节之间切换。

## 配置

配置示例见 `config.example.json`。本地配置可以放在 `config.json` 或 `config.local.json`，但不要提交真实密钥。

常用配置项：

- `root`：题库根目录。
- `answer_output`：答案复制/输出目录。
- `dashscope_api_key`：Qwen 识别用。
- `zhipuai_api_key`：视觉复筛用。
- `feishu_app_id`、`feishu_app_secret`、`feishu_verification_token`：飞书机器人用。
- `top_k`：粗筛默认候选数。

也可以用 Windows 用户环境变量配置飞书密钥，避免写进仓库。

## 验证

提交前建议跑：

```powershell
python scripts/smoke_test.py
```

它会检查题库 Excel、图片路径、荷载 JSON、路径修复逻辑和多 Agent/飞书基础流程。

## 注意

- 不要提交 `config.json`、`config.local.json` 里的真实密钥。
- 不要把 `.claude/settings.local.json` 当作项目改动提交。
- 自动章节只在有明确文字证据时使用；证据不足时应手动选择章节。
- 题库 Excel 路径失效时，程序会尝试在同章节题目目录下递归找图并修复路径。
