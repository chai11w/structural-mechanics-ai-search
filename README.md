# 结构力学搜题工具

面向结构力学答疑场景的本地题库检索工具。它可以从题目图片中识别外部荷载信息，再和本地 Excel 题库做相似度匹配，帮助快速找到相似题目和答案图片。

## 解决的问题

结构力学题库通常按章节、图片和答案文件散落在本地目录中。人工找题时需要先判断题型和荷载，再翻目录、对照答案，效率低且容易漏掉相似题。

这个工具把“看图识别荷载 -> 建立题库索引 -> 检索相似题 -> 打开答案”的流程串起来，适合答疑、刷题整理和题库维护。

## 核心功能

- 图片识别荷载：调用视觉模型从题目图片中提取集中力、均布荷载、弯矩等外部荷载。
- 相似题检索：按荷载类型和标注计算相似度，返回 Top K 相似题。
- 题目录入：支持把新题图片及识别到的荷载信息追加到章节 Excel。
- 答案定位：根据题目路径自动寻找对应答案图片。
- 桌面 GUI：支持图片检索、手动输入荷载、打开题目、打开答案。
- 剪贴板辅助：打开答案后可把答案图片复制到 Windows 剪贴板，方便粘贴到微信等场景。

## 技术点

- Python CLI + Tkinter 桌面端
- `pandas` / `openpyxl` 管理章节 Excel 题库
- 智谱 GLM 视觉模型识别题目图片中的荷载
- 自定义荷载归一化与相似度计算
- Windows 本地文件打开与剪贴板文件复制

## 项目结构

```text
.
├── build_index.py      # 批量扫描题目图片并生成章节 Excel 索引
├── search.py           # CLI 检索、储存、答案查询核心逻辑
├── gui.py              # Tkinter 桌面界面
├── 2静定结构.xlsx
├── 3静定结构位移.xlsx
├── 4力法.xlsx
├── 5位移法.xlsx
└── 6力矩分配.xlsx
```

## 使用方式

安装依赖：

```powershell
pip install zhipuai pandas openpyxl tkinterdnd2
```

设置 API Key：

```powershell
$env:ZHIPUAI_API_KEY="your-api-key"
```

启动桌面端：

```powershell
python gui.py
```

命令行检索：

```powershell
python search.py search --image "D:\path\to\question.jpg" --chapter "2静定结构"
```

手动输入荷载检索：

```powershell
python search.py search --loads '{"loads":[{"type":"集中","raw":"10kN"}]}' --chapter "2静定结构"
```

打开上次检索结果对应答案：

```powershell
python search.py answer --rank 1
```

## 面试可讲亮点

- 把真实学习/答疑中的重复流程产品化，而不是只写一个算法 demo。
- 题目图片先转成结构化荷载 JSON，再做可解释的相似度匹配。
- 同时考虑了录入、检索、答案打开、剪贴板复制等完整工作流。
- 对模型输出做了 JSON 容错、荷载类型修正和常见非荷载过滤。

## 注意

本项目依赖本地题库目录和个人 API Key。真实配置建议放在 `config.local.json` 或环境变量中，不要提交到仓库。
