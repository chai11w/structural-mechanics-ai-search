# 结构力学题库 AI 检索系统

这是一个面向结构力学答疑场景的本地题库检索系统。用户上传题目图片，或手动输入荷载信息后，系统会在结构力学题库中查找最相似的题目，并按排名打开对应答案。

项目重点不是简单的图片识别，而是把“题图识别、章节判断、荷载归一化、题库路由、相似度粗筛、视觉复筛、答案定位”串成一条可落地的工作流。

## 项目亮点

- 多入口使用：支持桌面 GUI、命令行和飞书机器人。
- 图片到检索链路：用 Qwen 识别题图中的荷载与可见题干信息，再进入本地题库检索。
- 保守自动章节：只有题图出现明确方法文字时才自动识别章节；证据不足时要求用户手动选择，避免跨章节乱搜。
- 主库 / 字母库分流：数值荷载题和未赋值字母荷载题分开维护，字母题通过编码归一化后复用相似度算法。
- Top 候选视觉复筛：粗筛后用视觉模型比较结构形状、荷载位置和方向，提高最终排序质量。
- 多题图处理：飞书端支持一张图片中包含多道题，先给出题号、章节、荷载摘要，再按用户选择逐题检索。
- 漏存审计与补库：GUI 一键扫描题目目录中未入库的图片，自动识别并写入主库或字母库，写入前会备份 Excel。

## 适用场景

这个项目来自真实结构力学答疑工作流，主要解决三类问题：

1. 题库题目越来越多，手动按文件夹找答案很慢。
2. 同一类题目在结构形状、荷载位置、字母/数值表达上有很多变体，纯文件名或关键词检索不可靠。
3. 手机端收到题图后，希望快速返回相似题和答案，而不是先保存图片、打开电脑、手动搜索。

## 系统流程

```mermaid
flowchart LR
    A["题目图片 / 手动荷载"] --> B["Qwen 荷载识别"]
    B --> C["章节判断"]
    C --> D["RuleRouter 路由"]
    D --> E["主库 Excel"]
    D --> F["字母库 Excel"]
    E --> G["荷载相似度粗筛"]
    F --> G
    G --> H["Zhipu 视觉复筛 Top 候选"]
    H --> I["返回相似题排名"]
    I --> J["按排名打开答案"]
```

## 目录说明

```text
.
├── gui.py                         # 桌面 GUI
├── search.py                      # 基础检索、储存、答案定位
├── multi_agent_pipeline.py        # Qwen 识别、路由、复筛协调
├── scripts/
│   ├── multi_agent_search.py      # 多 Agent CLI 检索
│   ├── search_by_loads.py         # 荷载检索 / 答案 CLI
│   ├── feishu_tiku_bot.py         # 飞书机器人
│   ├── audit_unindexed_questions.py
│   ├── store_unindexed_questions.py
│   └── smoke_test.py              # 只读验证
├── config.example.json            # 配置模板
└── requirements.txt
```

## 题库结构

当前按 5 个章节维护：

- `2静定结构`
- `3静定结构位移`
- `4力法`
- `5位移法`
- `6力矩分配`

主库保存数值荷载题和已赋值字母题，例如 `P=40kN`、`q=20kN/m`。

字母库保存未赋值字母题，例如 `q`、`2P/a`、`M`。这类题会写入相似度编码，同时保留原始字母标注，避免把不同量纲体系混在一起比较。

> 说明：题库图片、答案图片和真实配置属于本地资产，不随仓库公开。克隆仓库后需要在 `config.local.json` 中配置自己的题库路径和模型密钥。

## 快速开始

安装依赖：

```powershell
pip install -r requirements.txt
```

复制配置：

```powershell
copy config.example.json config.local.json
```

在 `config.local.json` 中填写：

```json
{
  "root": "D:\\path\\to\\question-bank",
  "answer_output": "D:\\path\\to\\answer-output",
  "dashscope_api_key": "",
  "zhipuai_api_key": "",
  "top_k": 5
}
```

启动桌面端：

```powershell
python gui.py
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

## GUI 功能

- 图片检索：选择或拖入题图后检索相似题。
- 手动荷载检索：直接输入荷载类型和标注。
- 自动识别章节：默认使用保守章节识别，识别失败时提示手动选择。
- 结果预览：显示 Top 候选、相似度、题图预览和答案入口。
- 储存单题：把当前题图识别后写入章节 Excel。
- 一键审查：扫描题目目录中未入库的图片，自动识别并补入主库或字母库，需复核的题目只写报告不自动入库。

## 飞书机器人

启动本地服务和临时公网隧道：

```powershell
.\启动结构力学题库.bat
```

飞书端支持：

- 发题图后自动处理并回复候选题。
- 回复 `1/2/3` 获取对应答案。
- 回复 `0` 结束当前检索。
- 回复 `a` 切换自动章节 / 手动章节模式。
- 多题图先返回题号和识别摘要，用户再按题号逐题检索。

## 漏存审计与补库

只扫描未入库题图，不写 Excel：

```powershell
python scripts/audit_unindexed_questions.py
```

预演自动补库：

```powershell
python scripts/store_unindexed_questions.py
```

确认后写入 Excel：

```powershell
python scripts/store_unindexed_questions.py --apply
```

写入前会备份被修改的 Excel 到 `backups/`。`special_unindexed_questions.json` 用于记录确认不参与题库检索的特殊题，审计时会自动排除。

## 验证

提交前建议运行：

```powershell
python scripts/smoke_test.py
```

它会只读检查题库 Excel、荷载 JSON、图片路径、路径修复逻辑、多 Agent 路由和飞书基础状态机。

## 技术取舍

- 不跨章节盲搜：结构力学不同章节的相似图形可能解法完全不同，因此章节不明确时宁可让用户选择。
- 粗筛和复筛分层：先用荷载相似度保证速度，再对少量候选做视觉复筛，平衡成本和准确率。
- 主库和字母库分离：未赋值字母题如果直接和数值题混搜，容易出现量纲混淆，所以单独路由。
- 自动补库保守写入：只有能明确路由到主库或字母库的题才自动追加，异常和混合情况进入报告等待人工复核。

## 安全说明

- 不要提交 `config.json`、`config.local.json`、`.env` 或真实 API key。
- 本地工具配置如 `.claude/settings.local.json` 不应提交。
- 飞书密钥建议使用环境变量配置。
