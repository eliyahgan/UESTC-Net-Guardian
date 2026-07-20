# UESTC 网络与热点守护

## 程序

打包后的程序名是 `UESTCNetGuardian.exe`，显示名为“UESTC 网络与热点守护”。它没有主窗口，只在 Windows 通知区域（系统托盘）显示图标。右键图标可以看到：

- `自动连接校园网`
- `自动保持热点`
- `开机自启动`
- `立即检查并修复`
- `打开 Windows 热点设置`
- `查看运行日志`
- `打开数据目录`
- `退出`

首次运行时两个守护模式默认开启；勾选状态保存在 `%LOCALAPPDATA%\UESTCNetGuardian\settings.json`。账号和密码仍只从项目目录的 `.env` 读取，不会写进 EXE。

## 热点行为

热点守护使用 Windows 原生 `NetworkOperatorTetheringManager` API：

1. 定期关闭系统的“无客户端自动关闭”超时（如果该开关处于开启状态）。
2. 每 10 秒读取当前热点状态。
3. 只有在状态为 `OFF` 时调用无参数 `StartTetheringAsync()`；无参数调用会沿用 Windows 设置中现有的共享方式、SSID、密码、频段和认证配置。
4. 网络切换、驱动重置或睡眠唤醒导致热点关闭时，自动重试；不会主动停止用户已经打开的热点。

关闭 `自动保持热点` 只会停止守护，不会主动关闭当前热点。

## 构建

在项目目录执行（PowerShell 执行策略限制时使用第二条命令）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt -r requirements-build.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build_guardian.ps1
```

输出目录：

```text
dist\UESTCNetGuardian\UESTCNetGuardian.exe
```

使用 `--onedir` 是为了让任务管理器只显示一个有辨识度的主进程，并避免 one-file 自解压带来的额外进程。

## 自启动迁移

程序菜单里的 `开机自启动` 使用当前用户的 HKCU `Run` 项，不需要管理员权限。启用新程序后，旧的 `UESTC AutoConnect.lnk` 启动快捷方式会被删除，避免旧 `pythonw.exe` 与新守护同时运行。

命令行也可以管理启动项：

```powershell
.\dist\UESTCNetGuardian\UESTCNetGuardian.exe --startup enable
.\dist\UESTCNetGuardian\UESTCNetGuardian.exe --startup disable
```

运行诊断（只读，不会启动或停止热点）：

```powershell
.\dist\UESTCNetGuardian\UESTCNetGuardian.exe --diagnostic-output .\diagnostic.json
```

日志默认位于 `%LOCALAPPDATA%\UESTCNetGuardian\UESTCNetGuardian.log`；若 Windows 环境禁止该目录写入，程序会自动回退到 EXE 目录下的 `.guardian-data`。
