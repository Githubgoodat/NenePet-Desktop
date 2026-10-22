"""Explicit Ctrl+Alt+N entry point, with no clipboard or keyboard simulation.

Windows RegisterHotKey owns the chord; MOD_NOREPEAT suppresses held-key repeat.
The native filter queues ``activated`` onto the Qt main thread. The UI must
check DND before reading a user-copied clipboard value or displaying its tool.
Neither registration nor activation reads text, captures a screen, or sends a
request. Other platforms report unavailable; the tray entry remains usable.

Call start on the Qt main thread and stop before application teardown. A stop
requested from another thread is disarmed immediately and unregistered on the
Qt thread. aboutToQuit and QObject destruction provide additional cleanup.

API references:
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-unregisterhotkey
https://doc.qt.io/archives/qt-5.15/qabstractnativeeventfilter.html
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import itertools
import sys
import weakref

from PyQt5.QtCore import (
    QAbstractNativeEventFilter, QCoreApplication, QObject, QThread, Qt,
    pyqtSignal, pyqtSlot,
)


WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
VIRTUAL_KEY_N = 0x4E
HOTKEY_MODIFIERS = MOD_ALT | MOD_CONTROL | MOD_NOREPEAT
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
_IDS = itertools.count(0x4E00)


class _WindowsHotkeyAPI:
    """Load User32 lazily, only for an explicitly started Windows instance."""

    def __init__(self):
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.UnregisterHotKey.restype = wintypes.BOOL
        self.last_error = 0

    def register(self, hotkey_id: int, modifiers: int, key: int) -> bool:
        ctypes.set_last_error(0)
        registered = bool(self._user32.RegisterHotKey(None, hotkey_id, modifiers, key))
        self.last_error = ctypes.get_last_error()
        return registered

    def unregister(self, hotkey_id: int) -> bool:
        ctypes.set_last_error(0)
        removed = bool(self._user32.UnregisterHotKey(None, hotkey_id))
        self.last_error = ctypes.get_last_error()
        return removed

    @staticmethod
    def decode_message(event_type, message):
        if bytes(event_type) not in (b"windows_dispatcher_MSG", b"windows_generic_MSG"):
            return None
        address = int(message) if message is not None else 0
        if not address:
            return None
        packet = ctypes.cast(address, ctypes.POINTER(wintypes.MSG)).contents
        if packet.message != WM_HOTKEY:
            return None
        parameters = int(packet.lParam)
        return int(packet.message), int(packet.wParam), parameters & 0xFFFF, (parameters >> 16) & 0xFFFF


class _Registration:
    """Plain cleanup state can outlive a destroyed Python QObject wrapper."""

    def __init__(self, hotkey_id):
        self.hotkey_id = hotkey_id
        self.app = self.api = self.filter = None
        self.registered = False

    def remove_filter(self):
        if self.filter is not None:
            self.app.removeNativeEventFilter(self.filter)
            self.filter = None

    def dispose(self):
        if self.registered:
            try:
                self.api.unregister(self.hotkey_id)
            except (OSError, RuntimeError, AttributeError):
                pass
            self.registered = False
        try:
            self.remove_filter()
        except (RuntimeError, AttributeError):
            pass


class _HotkeyEventFilter(QAbstractNativeEventFilter):
    def __init__(self, owner):
        super().__init__()
        self._owner = weakref.ref(owner)

    def nativeEventFilter(self, event_type, message):
        owner = self._owner()
        if owner is None or not owner.is_registered:
            return False, 0
        try:
            decoded = owner._state.api.decode_message(event_type, message)
            if decoded is None:
                return False, 0
            message_id, hotkey_id, modifiers, key = decoded
            if (message_id != WM_HOTKEY or hotkey_id != owner._state.hotkey_id
                    or (modifiers & ~MOD_NOREPEAT) != (MOD_ALT | MOD_CONTROL) or key != VIRTUAL_KEY_N):
                return False, 0
            owner._queue_activation()
            return True, 0
        except (RuntimeError, ValueError, TypeError, AttributeError, OSError, OverflowError):
            return False, 0


class UtilityHotkey(QObject):
    """Optional Windows global shortcut. Construction never registers anything.

    ``native_api`` implements register(id, modifiers, key), unregister(id),
    decode_message(event_type, message), and an integer last_error attribute.
    ``app`` supplies Qt thread, event-filter, and aboutToQuit interfaces. These
    are injectable so tests never need a real global registration.
    """

    activated = pyqtSignal()
    unavailable = pyqtSignal(str)
    _activation_requested = pyqtSignal(int)
    _stop_requested = pyqtSignal()
    shortcut_label = "Ctrl+Alt+N"

    def __init__(self, parent=None, *, native_api=None, app=None, platform_name=None):
        super().__init__(parent)
        self._state = _Registration(next(_IDS))
        self._native_override = native_api
        self._app_override = app
        self._platform = sys.platform if platform_name is None else platform_name
        self._generation = 0
        self._pending_generation = None
        self._accept_activation = False
        self._quit_connected = False
        self.last_error = ""
        self._activation_requested.connect(self._deliver_activation, Qt.QueuedConnection)
        self._stop_requested.connect(self._stop_on_main_thread, Qt.QueuedConnection)
        # Do not capture self: it may no longer have a live C++ QObject here.
        state = self._state
        self.destroyed.connect(lambda: state.dispose())

    @property
    def is_registered(self) -> bool:
        return self._state.registered

    def _notify(self, message):
        self.last_error = message
        self.unavailable.emit(message)

    def _on_main_thread(self, app):
        return QThread.currentThread() == app.thread()

    def start(self) -> bool:
        if self.is_registered:
            if self._accept_activation:
                return True
            self._notify("Ctrl+Alt+N 尚未完成释放，请先停止快捷键；可从托盘打开工具窗口。")
            return False
        if self._platform != "win32":
            self._notify("当前平台暂不支持 Ctrl+Alt+N 全局快捷键，可从桌宠托盘打开工具窗口。")
            return False
        app = self._app_override or QCoreApplication.instance()
        if app is None or not self._on_main_thread(app) or self.thread() != app.thread():
            self._notify("Ctrl+Alt+N 必须由 Qt 主线程启用，可从桌宠托盘打开工具窗口。")
            return False
        state = self._state
        if state.hotkey_id > 0xBFFF:
            self._notify("快捷键标识已用尽，可从桌宠托盘打开工具窗口。")
            return False
        state.app = app
        try:
            state.api = self._native_override if self._native_override is not None else _WindowsHotkeyAPI()
            state.filter = _HotkeyEventFilter(self)
            app.installNativeEventFilter(state.filter)
            if not state.api.register(state.hotkey_id, HOTKEY_MODIFIERS, VIRTUAL_KEY_N):
                code = int(getattr(state.api, "last_error", 0))
                state.remove_filter()
                reason = "已被其他程序占用" if code == ERROR_HOTKEY_ALREADY_REGISTERED else "注册失败"
                self._notify(f"Ctrl+Alt+N {reason}（系统错误 {code}），可从桌宠托盘打开工具窗口。")
                return False
            state.registered = True
            if not self._quit_connected:
                app.aboutToQuit.connect(self.stop)
                self._quit_connected = True
        except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
            state.dispose()
            self._notify("Ctrl+Alt+N 系统快捷键接口不可用，可从桌宠托盘打开工具窗口。")
            return False
        self._generation += 1
        self._pending_generation = None
        self._accept_activation = True
        self.last_error = ""
        return True

    def stop(self) -> None:
        self._accept_activation = False
        self._generation += 1
        self._pending_generation = None
        if not self.is_registered and self._state.filter is None:
            return
        if self._on_main_thread(self._state.app):
            self._stop_on_main_thread()
        else:
            self._stop_requested.emit()

    @pyqtSlot()
    def _stop_on_main_thread(self):
        state = self._state
        if state.registered:
            try:
                removed = state.api.unregister(state.hotkey_id)
            except (OSError, RuntimeError, AttributeError):
                removed = False
            if not removed:
                self._notify("Ctrl+Alt+N 已停用，但系统注销失败；退出桌宠后系统会释放该快捷键。")
                return  # Keep ownership so a later stop can retry the release.
            state.registered = False
        try:
            state.remove_filter()
        except (RuntimeError, AttributeError):
            self._notify("快捷键已注销，但事件过滤器未能移除；退出桌宠后会清理。")

    def _queue_activation(self):
        if not self.is_registered or not self._accept_activation:
            return
        if self._pending_generation == self._generation:
            return  # Coalesce duplicate native deliveries before the Qt turn.
        self._pending_generation = self._generation
        self._activation_requested.emit(self._generation)

    @pyqtSlot(int)
    def _deliver_activation(self, generation):
        if self._pending_generation == generation:
            self._pending_generation = None
        if self.is_registered and self._accept_activation and generation == self._generation:
            self.activated.emit()
