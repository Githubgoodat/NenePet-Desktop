"""A tray toggle backed by the Windows startup registration itself."""
from PyQt5.QtWidgets import QAction, QMessageBox

from aipet.core.autostart import AutostartError, AutostartManager, default_target


def create_autostart_action(parent, *, manager=None):
    action = QAction(parent)
    action.setCheckable(True)
    try:
        manager = manager or AutostartManager(default_target())
    except AutostartError:
        manager = None

    def refresh():
        action.blockSignals(True)
        try:
            state = manager.status() if manager is not None else "unavailable"
        except AutostartError:
            state = "unavailable"
        action.setChecked(state == "enabled")
        action.setEnabled(state in {"disabled", "enabled"})
        action.setToolTip("登录 Windows 后自动启动；取消勾选即可关闭。" if state in {"disabled", "enabled"}
                          else "自启入口不可用或同名项属于其他命令；没有修改现有注册项。")
        action.blockSignals(False)

    def toggle(checked):
        if manager is None:
            return
        try:
            manager.enable() if checked else manager.disable()
        except AutostartError:
            QMessageBox.warning(None, "开机启动", "无法安全更改开机启动；同名项可能属于其他命令，或当前注册表不可写。没有改动其他启动项。")
        refresh()

    action.toggled.connect(toggle)
    action.refresh_status = refresh
    refresh()
    return action
