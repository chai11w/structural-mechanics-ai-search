# 结构力学题库 AI 检索系统

这是一个面向结构力学答疑场景的本地题库检索系统。用户上传题目图片，或手动输入荷载信息后，系统会在结构力学题库中查找最相似的题目，并按排名打开对应答案。

项目重点不是简单的图片识别，而是把“题图识别、章节判断、荷载归一化、题库路由、相似度粗筛、视觉复筛、答案定位”串成一条可落地的工作流。

## 项目亮点

- 多入口使用：支持命令行、飞书机器人和隔离开发中的 Agent。
- 图片到检索链路：用 Qwen 识别题图中的荷载与可见题干信息，再进入本地题库检索。
- 适度放开的自动章节：先单独抄取图片中实际可见的题干；没有题干的纯结构图直接要求用户手动选择，有题干时再按明确方法文字、典型题型文字和结构信息识别章节，并全量记录章节判断样本用于后续优化。
- 主库 / 字母库分流：数值荷载题和未赋值字母荷载题分开维护，字母题通过编码归一化和结构类型筛选后复用相似度算法。
- 候选视觉复筛：粗筛后用视觉模型只比较主杆件骨架和整体轮廓，忽略荷载位置、方向、尺寸和文字；默认最多 10 候选并发复筛，单项超时会单独补评，仍不完整时整体回退粗筛；最终相似度仍按“粗筛荷载分 + 视觉轮廓分”的混合分控制输出质量。
- 多题图处理：飞书端支持一张图片中包含多道题，先给出题号、章节、荷载摘要，再按用户选择逐题检索。
- 题库维护闭环：支持漏存审计、飞书新增题目入库、候选错题删除；写入 live Excel 前会备份。

## 适用场景

这个项目来自真实结构力学答疑工作流，主要解决三类问题：

1. 题库题目越来越多，手动按文件夹找答案很慢。
2. 同一类题目在结构形状、荷载位置、字母/数值表达上有很多变体，纯文件名或关键词检索不可靠。
3. 手机端收到题图后，希望快速返回相似题和答案，而不是先保存图片、打开电脑、手动搜索。

## 独立 Agent 本地 Demo

新的 Agent 有独立的本地网页入口，不复用现有飞书机器人的端口、配置或运行状态：

```powershell
python -B scripts/run_tiku_agent_demo.py --port 8790
```

打开 `http://127.0.0.1:8790` 后可发题图或直接文字对话。页面采用单会话聊天画布：顶部菜单可打开临时会话抽屉，桌面和移动端入口一致，不伪造尚未实现的多会话历史。上传、拖放、候选题卡片选择、答案查看和图片大图预览均在同一条消息内完成；顶部栏和底部组合输入区固定，只有中间消息区滚动。

会话、上传题图、候选图、裁图和答案输出均位于 `.tmp_tiku_agent/`，媒体地址与当前 Cookie 会话绑定。上传原图、候选图和答案图在刷新或 Demo 重启后仍可显示，最后一次检索或对话操作 2 小时后统一过期；“新对话”会取消当前请求并清理前后端临时状态。前端会在上传前检查图片类型和 15MB 大小限制，并把服务端异常转换为可理解的中文提示。该入口会记录不含用户原话、图片路径或模型原文的结构化任务日志。

Demo 默认继续使用 Intent V1。Intent V2 只能通过显式开关在另一端口试运行：

```powershell
python -B scripts/run_tiku_agent_demo.py --port 8791 --intent-version v2
```

V2 默认使用独立的 `.tmp_tiku_agent_v2/`，其中的 SQLite、上传、会话文件、答案输出、模型缓存和结构化任务日志均不与 V1 共用。不要把试运行端口设为正在使用的 8790；需要回退时停止 8791 即可，原 V1 无需迁移或重启。

Intent V2 还提供受限的全局搜索兜底：只有章节判断失败、Agent 已明确提供该选项且用户明确同意时才会执行。它跨第 2–8 章收集粗筛分数 `>= 0.999` 的内容去重候选，以最多 10 路并发完成全部视觉复筛，只展示 `rerank_score > 0.95` 的结果并标注来源章节；结果仍是候选，必须由用户选择 `candidate_rank` 后才会读取答案。普通章节检索、Intent V1 和现有飞书机器人不走该流程。

## 系统流程

```mermaid
flowchart LR
    A["题目图片 / 手动荷载"] --> B["Qwen 荷载识别"]
    B --> C["章节判断"]
    C --> D["RuleRouter 路由"]
    D --> E["主库 Excel"]
    D --> F["字母库 Excel"]
    F --> G["结构类型筛选"]
    E --> H["荷载相似度粗筛"]
    G --> H
    H --> I["Zhipu 视觉复筛 Top 候选"]
    I --> J["返回相似题排名"]
    J --> K["按排名打开答案"]
```

## 目录说明

```text
.
├── search.py                      # 基础检索、储存、答案定位
├── multi_agent_pipeline.py        # Qwen 识别、路由、复筛协调
├── scripts/
│   ├── multi_agent_search.py      # 多 Agent CLI 检索
│   ├── search_by_loads.py         # 荷载检索 / 答案 CLI
│   ├── feishu_tiku_bot.py         # 飞书机器人
│   ├── feishu_store_flow.py       # 飞书新增题目入库
│   ├── feishu_delete_flow.py      # 飞书候选错题删除
│   ├── chapter_judgment_log.py    # 飞书章节判断 JSONL 日志
│   ├── audit_unindexed_questions.py
│   ├── store_unindexed_questions.py
│   └── smoke_test.py              # 只读验证
├── config.example.json            # 配置模板
└── requirements.txt
```

## 题库结构

当前按 7 个章节维护：

- `2静定结构`
- `3静定结构位移`
- `4力法`
- `5位移法`
- `6力矩分配`
- `7矩阵位移`
- `8影响线`

主库保存数值荷载题和已赋值字母题，例如 `P=40kN`、`q=20kN/m`。

字母库保存未赋值字母题，例如 `q`、`2P/a`、`M`。字母库 Excel 列为 `题目名称`、`荷载`、`结构类型`。这类题会写入相似度编码，同时保留原始字母标注，避免把不同量纲体系混在一起比较。

字母库检索会先按章节定位，再按 `梁`、`钢架`、`桁架`、`拱` 做结构类型筛选，最后按荷载相似度排序。结构类型优先从题干文字推断；题干不明确时才调用图像分类模型。飞书新增字母题时必须同步写入 `结构类型`，否则后续检索可能漏掉新题。

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
  "top_k": 3
}
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

## 飞书机器人

启动本地服务和临时公网隧道：

```powershell
.\启动结构力学题库.bat
```

飞书端支持：

- 发题图后自动处理并回复候选题。
- 回复实际显示的序号获取对应答案。
- 回复 `0` 结束当前检索。
- 回复 `a` 切换自动章节 / 手动章节模式。
- 多题图先返回题号和识别摘要，用户再按题号逐题检索。
- 回复 `+` 进入新增题目入库流程。
- 在候选页回复对应负数可删除错误候选，删除前会二次确认并备份。

飞书端每次章节判断都会写入 `data/feishu_chapter_failure_log.jsonl`，包括自动采用、需要手动和手动补章节样本。这个文件名沿用早期失败日志名，但现在用于全量观察章节判断效果。

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

- 不跨章节盲搜：结构力学不同章节的相似图形可能解法完全不同，因此纯结构图不自动猜章节；有明确方法词或典型题型文字时才自动采用。Intent V2 仅在章节未知并经用户明确授权后提供严格全局搜索兜底，不自动触发，也不降低阈值返回猜测。
- 粗筛和复筛分层：先用荷载相似度保证速度，再对少量候选做视觉复筛，平衡成本和准确率。
- 复筛结果不硬补 Top 3：复筛后相似比 `>90%` 的结果全部输出；如果没有 `>90%`，则 `>80%` 的结果最多输出 3 个；若没有结果超过 80%，只保留相似比最高的 1 个。初筛候选池和未复筛输出仍按原逻辑保留。
- 主库和字母库分离：未赋值字母题如果直接和数值题混搜，容易出现量纲混淆，所以单独路由。
- 自动补库保守写入：只有能明确路由到主库或字母库的题才自动追加，异常和混合情况进入报告等待人工复核。

## 安全说明

- 不要提交 `config.json`、`config.local.json`、`.env` 或真实 API key。
- 本地工具配置如 `.claude/settings.local.json` 不应提交。
- 飞书密钥建议使用环境变量配置。
