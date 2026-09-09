# 阶段 5 浏览器控制与恢复

状态：本地实现及隔离浏览器验收完成，未上线。阶段 5 整体验收仍按
[总计划](phase5_execution_plan.md) 执行，本文件不替代 A01～A18。

## 控制授权与读取

`GET /api/execution` 在同一个执行库事务中核对 owner，读取会话代次、
通用状态版本、父子状态和至多 20 条当前身份/代次的未完成操作。
它不等待模型持有的 Python 锁，也不执行恢复或新的模型调用。
过期运行租约只在响应中分类为 UNKNOWN；查询不会接管执行者。

新增 `execution_control` schema 1：`epoch`、`state_version`、`controls`、
`pending` 和 `has_more`。控制项仅有 `reset_session`、`stop_child`、
`finish_page`、`recover_operation`，目标使用对应的 workflow/child/unit/revision
或 source operation ID。未公开输入、客户端操作键、路径、模型原文及费用原始记录。
服务器最终执行时仍检查身份、代次、版本、目标、收据和输入版本。

这是执行控制的独立授权，V1 业务动作语义不变。父 A2_ACTIVE 已保存而子尚未
首次保存时，V1 会给出 ACTIVE_CHILD_TASK_MISSING 并禁用检索/选题动作；
控制面板可以接受这个合法的不一致快照，按独立控制授权停止或重置。
格式损坏的 V1、控制字段或不匹配的执行上下文仍整体拒绝。

CHECK_RECEIPT 只表示存在可以核对的子/批次收据，不能据此保证恢复成功。
未知外部副作用给出 UNCONFIRMED_EFFECT，没有收据给出 NO_RECEIPT；
两者不提供恢复按钮。实际恢复继续使用后端的文件、版本及调用确认检查。

## 浏览器行为

- 阶段 5 启用后显示“任务状态与控制”。页面启动只尝试一次独立状态读取，
  后续由用户刷新；没有自动重跑、后台轮询或暂停/继续按钮。
- 控制使用独立 Web Lock，不排在普通任务请求锁后面；不支持 Web Lock 时拒绝控制。
  普通业务入口继续使用现有锁和 pending fence。
- 每次控制先保存操作编号、代次、版本、精确目标和需要对账的 fence，再发请求。
  网络中断或超时保留记录，刷新页面后可点击“核对上次操作”，沿用原编号和原请求。
  记录损坏时拒绝新控制，不猜测旧请求内容。
- 控制成功必须收到完整 V1 和精确 fence ACK，才清除对应 pending 记录。
  确认被拒绝且事务已回滚的控制，仅清自己的 fence，不清继承的业务 fence。
- 成功或重复回执之后再读一次当前状态。历史回执不直接授权新的动作。
  跨标签通知及已清除的 fence 会使旧标签页失去提交/展示旧结果的资格。
- 停止子题保留整页其余题目；结束本页保留记录并关闭题目；新对话清空当前态并换代次。
  不承诺立即取消已经发出的模型请求。
- 修正了结束整页后的兼容投影：V1 的 CLOSED 可以来自 page_finished，
  无需把未检索题伪装成已检索。COMPLETED 仍优先于 CLOSED。

## 可复跑验证

自动测试：

```powershell
python -B -m unittest -q tests.test_execution_frontend tests.test_execution_handoffs
python -B -m unittest -q tests.test_demo_web_task_state
```

浏览器夹具使用全新的临时数据库、假题图、假模型和固定 localhost:8910；
不读取真实配置、live 题库或生产运行目录。只能用于隔离验收，运行后停止该进程。

```powershell
python -B -m tests.phase5_browser_fixture
```

另开终端，使用专用浏览器 session（脚本会关闭该 session 中其余测试页）：

```powershell
New-Item -ItemType Directory -Path output/playwright -Force
npx --yes --package @playwright/cli playwright-cli -s=phase5-acceptance open http://127.0.0.1:8910/ --headed
npx --yes --package @playwright/cli playwright-cli -s=phase5-acceptance --raw run-code --filename tests/phase5_browser_controls.js
npx --yes --package @playwright/cli playwright-cli -s=phase5-acceptance close
```

2026-09-09 的实际结果：普通浏览器请求锁仍占用且 provider 未释放时，另一标签页
停止成功；晚到流返回 error，父保持 WAIT_UNIT_SELECTION，子和当前题为空。
显式恢复前后模型替身识别计数均为 8（夹具累计计数，不是单次调用数）。
结束本页的两次请求使用相同操作编号和请求体，仅产生 1 条操作记录；
第二次请求来自刷新后的“核对上次操作”，确认后本地 journal 清除。
重置后 epoch 改变且 workflow 不存在。390×844 截图无横向溢出，已人工查看。

输出为 `output/playwright/phase5-controls-result.txt` 和
`output/playwright/phase5-controls-mobile.png`，均为临时验收材料，不加入代码提交。
两份材料另存于仓库外 `F:\cc\_backups\7-题库检索\2026-09-09\phase5-browser-controls`。
浏览器控制台包含 favicon 404，以及主动丢弃响应时预期的网络错误；
这些不作为脚本异常或业务成功依据。断言检查实际 HTTP、SQLite 和浏览器状态。

完整回归 1561 项通过，113.656 秒。此前首轮两项失败均来自旧静态资源版本号
断言，更新后相关 42 项及上述完整回归通过。完整回归后补充“已存空闲 A2 状态
不能发布空 task_id 的停止目标”守卫及用例，27 项控制/前端相关测试通过，5.673 秒。
最后这项小改动没有再次运行全仓；完整日志和末次定向日志分别为
`.tmp_phase5_1/browser_controls_full_final.txt`、`.tmp_phase5_1/control_idle_final.txt`。
