# Security Policy

## 报告安全问题

请不要在公开 Issue 中发布以下内容：

- `.env` 文件或校园网账号、密码
- 未经检查的完整日志
- challenge、token、info、checksum 或完整门户查询参数
- 热点密码、设备名、MAC 地址或可识别个人身份的信息

如果问题不包含敏感信息，可以提交公开 Issue，并提供：

- Windows 版本
- 程序版本
- 已脱敏的错误描述
- 可以公开的最小复现步骤

若必须附带敏感信息，请优先使用 GitHub Security Advisory 的私密报告功能（仓库启用后），或者等待维护者提供私密联系渠道。

## 凭据处理

真实凭据只应保存在本地 `.env` 中。该文件已由 `.gitignore` 排除，但在提交前仍应运行 `git status` 确认它没有被暂存。若凭据曾被提交或上传，应立即更改密码；仅从最新提交中删除文件不足以清除 Git 历史中的副本。

## 支持范围

当前主要维护 `UESTCNetGuardian` 托盘程序与 `uestc_srun_autoconnect.py` Srun 客户端。保留的旧 ePortal 实现仅用于来源记录，不作为推荐入口。
