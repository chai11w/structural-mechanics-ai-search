# 发布交接：canonical Git blob 镜像修复

## 问题

第一次整理到 Git 分支时，`mainline_mirror/source` 的 81 个文件来自启用
`core.autocrlf=true` 的 Windows 工作树，manifest 也按 CRLF 字节生成。它们虽然在当前机器上
自洽，却不等于来源 commit `bc27cba1339f8a73aee18c4a44e109cecd84bd3d` 的 canonical Git
blob；提交后在 Linux checkout 会变为 LF，从而导致 integrity 全量失败。

## 修复方式

- 对 manifest 中每个 path 先用 `git rev-parse <commit>:<path>` 取得 blob OID。
- 用 `git cat-file blob <oid>` 的原始 stdout byte stream 直接写入镜像文件，不经过工作树过滤、
  PowerShell 文本编码或 archive 解压转换。
- 按修复后的 canonical 文件字节重新生成 manifest SHA-256。
- 逐项验证：
  - `git hash-object --no-filters <mirror-file>` 等于来源 commit blob OID；
  - 镜像文件 SHA-256 等于 manifest `sha256`。

## 最终验证

- canonical blob OID：81/81 匹配。
- manifest SHA-256：81/81 匹配。
- `mainline_mirror.integrity.verify_snapshot()`：81/81 通过。
- `tests/mainline_parity`：12/12 通过。
- 镜像核心 `test_tiku_agent*.py`：164/164 通过。
- `node --check mainline_mirror/observation/web_static/observer.js`：通过。
- 禁带文件审计：0 个；测试生成的 runtime、data 和 `__pycache__` 已物理清除。
- 未执行 git add、commit 或 push。

## 注意

不要再从启用自动换行转换的普通工作树复制文件来更新该镜像。后续更新来源 commit 时，必须继续
从 Git 对象库提取原始 blob，并同时做 blob OID 与 manifest SHA-256 双重校验。
