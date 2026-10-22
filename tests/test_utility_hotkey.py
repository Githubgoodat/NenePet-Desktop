"""Native-fake shortcut tests: never register keys or access the clipboard."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QCoreApplication, QEvent, QThread
from PyQt5.QtWidgets import QApplication
from PyQt5 import sip

from aipet.ui.utility_hotkey import (
    ERROR_HOTKEY_ALREADY_REGISTERED, HOTKEY_MODIFIERS, MOD_ALT, MOD_CONTROL,
    MOD_NOREPEAT, VIRTUAL_KEY_N, WM_HOTKEY, UtilityHotkey, _WindowsHotkeyAPI,
)


class Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self):
        for slot in list(self.slots):
            slot()


class App:
    def __init__(self, qt):
        self.qt = qt
        self.aboutToQuit = Signal()
        self.filters = []
        self.installs = 0
        self.removals = 0

    def thread(self):
        return self.qt.thread()

    def installNativeEventFilter(self, event_filter):
        self.filters.append(event_filter)
        self.installs += 1

    def removeNativeEventFilter(self, event_filter):
        self.filters.remove(event_filter)
        self.removals += 1


class Native:
    def __init__(self):
        self.register = Mock(return_value=True)
        self.unregister = Mock(return_value=True)
        self.last_error = 0

    def decode_message(self, event_type, message):
        return message if event_type == b"windows_dispatcher_MSG" else None


class UtilityHotkeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.addCleanup(lambda: QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete))
        self.app, self.native = App(self.qt), Native()
        self.hotkey = UtilityHotkey(native_api=self.native, app=self.app, platform_name="win32")
        self.addCleanup(self.hotkey.stop)
        self.addCleanup(lambda: self.hotkey.deleteLater() if not sip.isdeleted(self.hotkey) else None)
        self.activations, self.errors = [], []
        self.hotkey.activated.connect(lambda: self.activations.append(threading.get_ident()))
        self.hotkey.unavailable.connect(self.errors.append)
        # A test omission must never load the real User32 adapter.
        self.enterContext(patch("aipet.ui.utility_hotkey._WindowsHotkeyAPI", side_effect=AssertionError("Real native registration forbidden")))

    def flush(self):
        self.qt.processEvents()

    def packet(self, *, key=VIRTUAL_KEY_N, modifiers=MOD_ALT | MOD_CONTROL, identifier=None, message=WM_HOTKEY):
        return (message, self.hotkey._state.hotkey_id if identifier is None else identifier, modifiers, key)

    def emit_native(self, packet=None):
        return self.hotkey._state.filter.nativeEventFilter(b"windows_dispatcher_MSG", self.packet() if packet is None else packet)

    def test_construction_is_inert_and_start_stop_are_idempotent(self):
        self.assertFalse(self.hotkey.is_registered)
        self.native.register.assert_not_called()
        self.hotkey.stop()
        self.assertTrue(self.hotkey.start())
        self.assertTrue(self.hotkey.start())
        self.native.register.assert_called_once_with(self.hotkey._state.hotkey_id, HOTKEY_MODIFIERS, VIRTUAL_KEY_N)
        self.assertTrue(HOTKEY_MODIFIERS & MOD_NOREPEAT)
        self.assertEqual(self.hotkey.shortcut_label, "Ctrl+Alt+N")
        self.assertEqual(self.app.installs, 1)
        self.hotkey.stop()
        self.hotkey.stop()
        self.native.unregister.assert_called_once_with(self.hotkey._state.hotkey_id)
        self.assertEqual(self.app.filters, [])
        self.assertEqual(self.app.removals, 1)
        self.assertFalse(self.hotkey.is_registered)

    def test_activation_is_queued_and_coalesces_duplicate_native_deliveries(self):
        self.hotkey.start()
        for _ in range(20):
            self.assertEqual(self.emit_native(), (True, 0))
        self.assertEqual(self.activations, [])
        self.flush()
        self.assertEqual(self.activations, [threading.get_ident()])
        # A separate physical press can activate again; no arbitrary cooldown.
        self.emit_native()
        self.flush()
        self.assertEqual(len(self.activations), 2)

    def test_native_delivery_from_worker_still_emits_activation_on_qt_main_thread(self):
        self.hotkey.start()
        worker = threading.Thread(target=self.emit_native)
        worker.start()
        worker.join()
        self.assertEqual(self.activations, [])
        self.flush()
        self.assertEqual(self.activations, [threading.get_ident()])

    def test_queued_activation_before_stop_is_discarded(self):
        self.hotkey.start()
        old_filter = self.hotkey._state.filter
        self.emit_native()
        self.hotkey.stop()
        self.flush()
        self.assertEqual(self.activations, [])
        self.assertEqual(old_filter.nativeEventFilter(b"windows_dispatcher_MSG", self.packet()), (False, 0))

    def test_old_generation_cannot_activate_restarted_registration(self):
        self.hotkey.start()
        self.emit_native()
        self.hotkey.stop()
        self.hotkey.start()
        self.emit_native()
        self.flush()
        self.assertEqual(len(self.activations), 1)
        self.assertEqual(self.native.register.call_count, 2)

    def test_unrelated_hotkey_id_modifier_key_and_event_type_pass_through(self):
        self.hotkey.start()
        packets = [self.packet(identifier=1), self.packet(key=ord("X")), self.packet(modifiers=MOD_CONTROL),
                   self.packet(modifiers=MOD_ALT | MOD_CONTROL | 4), self.packet(message=0x100)]
        for packet in packets:
            self.assertEqual(self.emit_native(packet), (False, 0))
        self.assertEqual(self.hotkey._state.filter.nativeEventFilter(b"xcb_generic_event_t", self.packet()), (False, 0))
        self.flush()
        self.assertEqual(self.activations, [])

    def test_register_conflict_reports_tray_fallback_and_releases_filter(self):
        self.native.register.return_value = False
        self.native.last_error = ERROR_HOTKEY_ALREADY_REGISTERED
        self.assertFalse(self.hotkey.start())
        self.assertIn("占用", self.errors[-1])
        self.assertIn("1409", self.errors[-1])
        self.assertIn("托盘", self.errors[-1])
        self.assertEqual(self.app.filters, [])
        self.assertFalse(self.hotkey.is_registered)
        self.native.unregister.assert_not_called()

    def test_other_native_failure_is_explicit_without_throwing(self):
        self.native.register.return_value = False
        self.native.last_error = 5
        self.assertFalse(self.hotkey.start())
        self.assertIn("注册失败", self.errors[-1])
        self.assertIn("5", self.errors[-1])
        self.assertNotIn("占用", self.errors[-1])

    def test_api_exception_removes_filter_and_does_not_break_startup(self):
        self.native.register.side_effect = OSError("synthetic API unavailable")
        self.assertFalse(self.hotkey.start())
        self.assertIn("接口不可用", self.errors[-1])
        self.assertEqual(self.app.filters, [])
        self.assertFalse(self.hotkey.is_registered)

    def test_other_platform_never_attempts_native_registration(self):
        self.hotkey._platform = "linux"
        self.assertFalse(self.hotkey.start())
        self.assertIn("平台暂不支持", self.errors[-1])
        self.native.register.assert_not_called()
        self.assertEqual(self.app.filters, [])

    def test_start_off_main_thread_is_rejected_without_registering(self):
        results = []
        worker = threading.Thread(target=lambda: results.append(self.hotkey.start()))
        worker.start()
        worker.join()
        self.flush()
        self.assertEqual(results, [False])
        self.native.register.assert_not_called()
        self.assertIn("主线程", self.errors[-1])

    def test_worker_stop_disarms_immediately_and_unregisters_on_main_loop(self):
        self.hotkey.start()
        self.emit_native()
        native_threads = []
        self.native.unregister.side_effect = lambda _identifier: native_threads.append(threading.get_ident()) or True
        worker = threading.Thread(target=self.hotkey.stop)
        worker.start()
        worker.join()
        self.native.unregister.assert_not_called()
        self.flush()
        self.assertEqual(self.activations, [])
        self.assertEqual(native_threads, [threading.get_ident()])
        self.assertFalse(self.hotkey.is_registered)

    def test_unregister_failure_disables_activation_and_allows_release_retry(self):
        self.hotkey.start()
        self.native.unregister.return_value = False
        self.hotkey.stop()
        self.assertTrue(self.hotkey.is_registered, "Do not claim OS registration was released")
        self.assertIn("注销失败", self.errors[-1])
        self.emit_native()
        self.flush()
        self.assertEqual(self.activations, [])
        self.assertFalse(self.hotkey.start())
        self.native.unregister.return_value = True
        self.hotkey.stop()
        self.assertFalse(self.hotkey.is_registered)
        self.assertEqual(self.app.filters, [])

    def test_application_quit_unregisters_exactly_once(self):
        self.hotkey.start()
        self.app.aboutToQuit.emit()
        self.app.aboutToQuit.emit()
        self.native.unregister.assert_called_once()
        self.assertFalse(self.hotkey.is_registered)

    def test_qobject_destruction_unregisters_when_caller_omits_stop(self):
        self.hotkey.start()
        self.hotkey.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.native.unregister.assert_called_once()
        self.assertFalse(self.hotkey.is_registered)
        self.assertEqual(self.app.filters, [])

    def test_multiple_instances_use_distinct_identifiers_and_own_cleanup(self):
        second_native = Native()
        second = UtilityHotkey(native_api=second_native, app=self.app, platform_name="win32")
        self.addCleanup(second.stop)
        self.addCleanup(lambda: second.deleteLater() if not sip.isdeleted(second) else None)
        self.assertTrue(self.hotkey.start())
        self.assertTrue(second.start())
        self.assertNotEqual(self.hotkey._state.hotkey_id, second._state.hotkey_id)
        second.stop()
        self.native.unregister.assert_not_called()
        self.assertTrue(self.hotkey.is_registered)

    def test_decoder_uses_only_local_msg_memory_without_loading_user32(self):
        # Exercise the native MSG layout without constructing the User32 API.
        api_type = _WindowsHotkeyAPI
        packet = wintypes.MSG()
        packet.message, packet.wParam = WM_HOTKEY, 0x4E00
        packet.lParam = (VIRTUAL_KEY_N << 16) | MOD_ALT | MOD_CONTROL
        actual = api_type.decode_message(b"windows_dispatcher_MSG", ctypes.addressof(packet))
        self.assertEqual(actual, (WM_HOTKEY, 0x4E00, MOD_ALT | MOD_CONTROL, VIRTUAL_KEY_N))
        self.assertIsNone(api_type.decode_message(b"xcb_generic_event_t", ctypes.addressof(packet)))
        self.assertIsNone(api_type.decode_message(b"windows_dispatcher_MSG", None))
        packet.message = 0x100
        self.assertIsNone(api_type.decode_message(b"windows_dispatcher_MSG", ctypes.addressof(packet)))


if __name__ == "__main__":
    unittest.main()
