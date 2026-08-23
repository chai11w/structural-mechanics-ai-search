# 8897 分流版本记录

测试集：`D:\桌面\新建文件夹`，A1=4、A2=3、A3=10，共 17 张。模型均为 Qwen `qwen3.7-plus`；原始评测结果和离线门禁重放结果分别保存于 `.tmp_tests\routing_8897_*.json`。

| 版本 | 规则重点 | 原始/重放结果 | 部署状态 |
| --- | --- | --- | --- |
| V1 | 只增加题图边界字段；边界不清阻止 A2，多题优先 A3；截断允许落 A3 | 首轮 `15/17`；不含截断样本 `15/16` | 可选回退 |
| V2 | 在 V1 上强制从自由说明纠正主结构截断，并把残缺单元判 A1 | 完整集 `13/17`，出现 A2 回归 | 禁止部署 |
| V3 | 保留 V1 的 A2/A3 边界；只纠正模型自相矛盾的“相邻题残片”字段；截断不强制 A1 | 当前保存输出离线重放 `16/17`；排除截断样本 `16/16` | 8897 默认 |

## 选择

8897 默认使用 V3。启动时可显式选择：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/tiku_agent_watchdog_8897.ps1 -TriagePolicyVersion v1
powershell -ExecutionPolicy Bypass -File scripts/tiku_agent_watchdog_8897.ps1 -TriagePolicyVersion v2
powershell -ExecutionPolicy Bypass -File scripts/tiku_agent_watchdog_8897.ps1 -TriagePolicyVersion v3
```

V3 的验收优先级是：不误放带相邻题残片的图片进入 A2；不破坏原有清晰 A2；截断图进 A3 可以接受。

## 统计口径

以上数字按保存的模型原文重新运行对应版本的本地门禁得到，不能直接使用 JSON 中调用当时写入的 `final_route` 字段。V3 唯一排除项是新增的截断图：
`A1/Snipaste_2026-08-23_16-44-28.png`。因此当前关注的非截断样本为 `16/16`，没有清晰 A2 回归。
