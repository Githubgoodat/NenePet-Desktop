# 开机启动

Windows 中可以从宁宁的托盘菜单或人物右键菜单勾选“开机启动”。它在**当前用户登录 Windows 后**启动，不是尚未登录时运行的系统服务；无需管理员权限。取消勾选即可关闭。

启动项只使用 `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run` 下的 `NenePetDesktop` 值，不枚举、删除或改写其他启动项。只有命令与当前安装入口完全一致时才把它视为本程序所有；同名但内容不同的值会显示冲突，不覆盖或删除。

- 安装版优先使用安装目录中的 `宁宁桌宠-原版交互.exe`，由已有启动器恢复 Viewer 和桌宠。
- 冻结分享版使用当前程序本身；它的现有启动流程会读取自己的 Viewer 设置。
- 公开源码使用 `pythonw.exe packaging/share_main.py`，通过分享启动入口恢复 Viewer。不是直接执行缺少显示程序准备的裸人物脚本，也不使用不存在的 `python -m aipet` 入口。

路径按 Windows 原生命令行规则引用，不通过 PowerShell、cmd.exe 或批处理解释。脚本模式要求已有 `pythonw.exe`，避免登录时弹出控制台。Microsoft 对 Run 命令行规定了 260 字符上限，因此过长的命令会被拒绝。

源码维护或手动核对可用管理工具，先执行只读状态检查：

```powershell
.\.venv\Scripts\python.exe tools/manage_autostart.py status
.\.venv\Scripts\python.exe tools/manage_autostart.py enable
.\.venv\Scripts\python.exe tools/manage_autostart.py disable
```

可显式指定已安装的原生启动器，例如 `--launcher 'C:\NenePet\宁宁桌宠-原版交互.exe'`；脚本入口则同时提供 `--launcher <完整.py路径> --pythonw <完整pythonw.exe路径>`。状态输出为 JSON，命令默认不执行桌宠，不重启 Windows，也不改写用户对话和模型设置。

如果移动了安装目录或换了解释器，请先在原目录关闭自启，再从新目录重新启用。Windows 可能延迟登录后的启动；Steam、Viewer 授权、显卡及本地模型准备仍遵循原启动流程。这里只能核对注册值和入口，不能以静态检查冒充一次真实重启验收。

参考：[Microsoft Run / RunOnce 注册表说明](https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys)。
