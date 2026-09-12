# 飞书统一写入的事件入口

保留飞书存题与删除功能，最终接入和力答相同的固定计划、永久题号、一次确认和私有写入器。当前提交完成事件身份校验及旧直接写入边界；持久草稿、写入器适配、原确认交互和回执仍需继续接通。没有修改正在运行的飞书服务或配置，不能凭此文启用受管生产写入。

## 已实现

`scripts/feishu_event_security.py` 在解析业务身份前校验原始 HTTP 字节上的 SHA-256 签名，检查时间戳、nonce、请求大小和重复签名头。配置加密推送时解密 AES-256-CBC/PKCS#7 事件，校验 Verification Token、应用 ID、真实 user/open_id 及消息身份；拒绝重复 JSON 字段和混合加密外壳。校验令牌在交给下游前移除。

协议依据为飞书官方 [接收事件说明](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/encrypt-key-encryption-configuration-case)、官方 SDK 的 [事件校验](https://github.com/larksuite/node-sdk/blob/main/dispatcher/request-handle.ts) 与 [AES 解密](https://github.com/larksuite/node-sdk/blob/main/utils/aes-cipher.ts)。签名使用原始请求正文，不把解析后的 JSON 重新序列化后验签。

`FeishuHandler` 将有界原始正文和原始请求头交给验证器。严格模式下，直接调用旧 `handle_payload` 不能绕过验签。通过验证的发送者身份在处理线程中显式绑定，离开当前处理即撤销；不能把正文里的 owner 标志或收费管理员自动登记当作维护权限。

新增、补答案及删除入口在配置维护者或启用 `TIKU_BANK_STORE` 时要求当前已验证身份属于独立维护者清单。受管模式的原 `FeishuStoreService.apply_plan` 和 `FeishuDeleteService.apply_plan` 拒绝直接写 Excel/图片，后续由适配器调用统一写入器；这项限制不等于飞书存储改造已完成。未配置这些新模式时保留旧行为，正式迁移前仍须治理所有实际旧写者。

## 配置与部署前提

- `FEISHU_ENCRYPT_KEY` 或服务端私有配置 `feishu_encrypt_key`：事件签名/加密密钥。
- 现有 Verification Token 和 app_id 必须同时配置。
- `feishu_bank_maintainer_ids`：显式允许维护题库的飞书 open_id 列表，与费用查询管理员分开。
- 配置加密密钥、维护者清单或 `TIKU_BANK_STORE` 任一项时启用严格事件入口；配置缺失时拒绝启动，不退回无校验模式。
- 加密解码使用当前题库 Python 环境已有的 `cryptography`。独立服务环境必须预检此依赖及系统时钟。

下一步需要持久接收队列、草稿与附件、消息去重、确认与确切计划及展示时点的绑定、断线重启恢复和真实回执；现有 `RecentEventIdCache` 仍只是内存去重。飞书确认应直接批准本批固定计划，不要求转到力答再次确认。正式接入还需要真实维护者配置、目录权限和单独的发布授权。
