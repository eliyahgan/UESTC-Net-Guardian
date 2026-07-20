# 本机部署说明

> 2026-07 起已迁移到带托盘和移动热点守护的 `UESTCNetGuardian.exe`。新部署方式见 [GUARDIAN_DEPLOYMENT.md](GUARDIAN_DEPLOYMENT.md)；下文保留旧命令行客户端说明供排错使用。

本目录使用 Python 3.13 虚拟环境按源码运行。`requirements-runtime.txt` 只包含直接运行依赖，`requirements-lock.txt` 记录本次实际安装的完整版本；上游的 `pyinstaller==6.5.0` 与 Python 3.13 不兼容，因此未用于本机常驻部署。

## 完成账号配置

只在本机编辑 `.env`，填写统一身份认证信息：

```text
UESTC_USERNAME=你的学号或工资号
UESTC_PASSWORD=你的统一身份认证密码
```

当前 `/srun_portal_pc` 门户明确要求学号/工资号，不使用手机号。旧的 `UESTC_PHONE` 变量只为兼容旧配置；设置 `UESTC_USERNAME` 后会优先使用新变量。

不要把密码发到聊天、终端参数或 Git。`.env` 是明文文件；学校门户使用 HTTP，因此请充分考虑密码复用风险。

校方当前入口使用 HTTP。安全加固版默认拒绝通过明文 HTTP 发送凭据。如果你确认当前门户确实只能使用 HTTP，并接受同一网络中的窃听风险，再把下面一项改为 `1`：

```text
UESTC_ALLOW_INSECURE_HTTP=1
```

## 首次前台验证

在 PowerShell 中运行：

```powershell
.\run-autoconnect.cmd --debug
```

先在已经联网的状态确认脚本通过 `/cgi-bin/rad_user_info` 判定在线；随后仅在 UESTC 校园网内测试认证。凭据被明确拒绝时，客户端会停止，不会持续撞库式重试。

## 开机启动

首次认证成功后运行：

```powershell
.\manage-startup.cmd
```

移除启动项：

```powershell
.\manage-startup.cmd remove
```

启动项使用绝对路径并固定工作目录，因此开机启动时能正确找到 `.env` 和日志文件。入口不依赖修改 PowerShell 执行策略。

## 本地安全加固

- 运行入口已切换为 `uestc_srun_autoconnect.py`；上游旧 ePortal 脚本仅保留作来源记录，不再启动。
- 在线状态以 Srun `/cgi-bin/rad_user_info` 为准，ping 不再作为认证依据。
- 登录按现网 `/cgi-bin/get_challenge` + `/cgi-bin/srun_portal` 协议生成 HMAC-MD5、XEncode 和 SHA1；原始密码不会进入 URL。
- 302、短 HTML 或模糊的 2xx 不再判定成功；必须由门户返回明确成功并通过在线状态复核。
- API 禁止自动重定向，目标必须与已验证的 UESTC 门户同主机。
- 日志使用追加式轮转文件，隐藏账号、密码、token、info、checksum 和查询参数值。
- 失败采用 15–300 秒指数退避；凭据错误或账号锁定时立即停止。
- Windows 使用命名互斥锁防止重复实例；循环异常会被记录并退避重试，不再静默退出。
- `.env` 和现有日志文件的 Windows ACL 已收紧为当前用户、SYSTEM、管理员及部署沙箱可访问。
