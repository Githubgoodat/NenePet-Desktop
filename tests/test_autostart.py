"""Fake HKCU Run only; no actual startup entry, viewer or process is changed."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aipet.core.autostart import (
    AutostartError, AutostartManager, RUN_KEY, StartupTarget, VALUE_NAME,
    default_target, target_for_entry,
)


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = "fixture-current-user"
    KEY_READ, KEY_QUERY_VALUE, KEY_SET_VALUE, REG_SZ = 1, 2, 4, 1

    def __init__(self):
        self.values = {"OtherApp": ("unrelated.exe --keep", self.REG_SZ)}
        self.key_exists = False
        self.writes = []
        self.denied = False

    def OpenKey(self, hive, key, reserved, rights):
        assert (hive, key, reserved) == (self.HKEY_CURRENT_USER, RUN_KEY, 0)
        if self.denied:
            raise PermissionError("private fixture error")
        if not self.key_exists:
            raise FileNotFoundError
        return FakeKey()

    def CreateKeyEx(self, hive, key, reserved, rights):
        assert (hive, key, reserved) == (self.HKEY_CURRENT_USER, RUN_KEY, 0)
        if self.denied:
            raise PermissionError("private fixture error")
        self.key_exists = True
        return FakeKey()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        self.writes.append(("set", name))
        self.values[name] = value, kind

    def DeleteValue(self, key, name):
        self.writes.append(("delete", name))
        del self.values[name]


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        self.target = StartupTarget((r"C:\Fixture Project\宁宁.exe",))
        self.manager = AutostartManager(self.target, registry=self.registry, windows=True)

    def test_status_does_not_create_a_key_or_write_configuration(self):
        self.assertEqual(self.manager.status(), "disabled")
        self.assertFalse(self.registry.key_exists)
        self.assertEqual(self.registry.writes, [])

    def test_enable_disable_are_idempotent_and_preserve_other_startup_items(self):
        other = self.registry.values["OtherApp"]
        self.assertTrue(self.manager.enable())
        self.assertEqual(self.manager.status(), "enabled")
        self.assertEqual(self.registry.values[VALUE_NAME], (self.target.command, self.registry.REG_SZ))
        self.assertFalse(self.manager.enable())
        self.assertTrue(self.manager.disable())
        self.assertEqual(self.manager.status(), "disabled")
        self.assertFalse(self.manager.disable())
        self.assertEqual(self.registry.values["OtherApp"], other)
        self.assertEqual(self.registry.writes, [("set", VALUE_NAME), ("delete", VALUE_NAME)])

    def test_conflicting_command_or_registry_type_is_never_adopted_or_removed(self):
        for value in (("someone-else.exe", 1), (self.target.command, 2), (42, 1)):
            with self.subTest(value=value):
                self.registry.key_exists = True
                self.registry.values[VALUE_NAME] = value
                self.assertEqual(self.manager.status(), "conflict")
                for operation in (self.manager.enable, self.manager.disable):
                    with self.assertRaises(AutostartError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "foreign_entry")
                self.assertEqual(self.registry.values[VALUE_NAME], value)
                self.assertFalse(self.registry.writes)

    def test_unsupported_and_registry_errors_do_not_leak_raw_os_details(self):
        manager = AutostartManager(self.target, registry=self.registry, windows=False)
        self.assertEqual(manager.status(), "unsupported")
        for operation in (manager.enable, manager.disable):
            with self.assertRaises(AutostartError) as caught:
                operation()
            self.assertEqual(str(caught.exception), "unsupported")
        self.registry.denied = True
        for operation in (self.manager.status, self.manager.enable, self.manager.disable):
            with self.assertRaises(AutostartError) as caught:
                operation()
            self.assertEqual(str(caught.exception), "registry_unavailable")
        self.assertEqual(self.registry.writes, [])

    def test_command_uses_windows_quoting_without_shell_interpreters(self):
        self.assertEqual(self.target.command, '"C:\\Fixture Project\\宁宁.exe"')
        target = StartupTarget((r"C:\Some Python\pythonw.exe", r"C:\Project & extras\packaging\share_main.py"))
        self.assertEqual(target.command, '"C:\\Some Python\\pythonw.exe" "C:\\Project & extras\\packaging\\share_main.py"')
        with self.assertRaises(AutostartError) as caught:
            AutostartManager(StartupTarget(("x" * 261,)), registry=self.registry, windows=True)
        self.assertEqual(caught.exception.code, "command_too_long")
        self.assertFalse(self.registry.key_exists)

    def test_source_entry_requires_existing_windowless_python_and_default_prefers_installed_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            share = root / "packaging/share_main.py"
            share.parent.mkdir()
            share.write_text("# fixture", encoding="utf-8")
            pythonw = root / "Python runtime/pythonw.exe"
            pythonw.parent.mkdir()
            pythonw.write_bytes(b"fixture")
            self.assertEqual(target_for_entry(share, pythonw=pythonw).arguments, (str(pythonw.resolve()), str(share.resolve())))
            python = pythonw.with_name("python.exe")
            python.write_bytes(b"fixture")
            with self.assertRaises(AutostartError) as caught:
                target_for_entry(share, pythonw=python)
            self.assertEqual(caught.exception.code, "windowless_python_required")
            native = root / "宁宁桌宠-原版交互.exe"
            native.write_bytes(b"fixture")
            self.assertEqual(default_target(root).arguments, (str(native.resolve()),))
            with patch("aipet.core.autostart.sys.frozen", True, create=True), patch("aipet.core.autostart.sys.executable", str(native)):
                self.assertEqual(default_target().arguments, (str(native.resolve()),))

    def test_missing_or_unsafe_entry_is_rejected_before_registry_access(self):
        for entry in (Path("relative.exe"), Path(r"\\fixture\share\program.exe"), Path(r"C:\missing\launcher.exe")):
            with self.subTest(entry=entry), self.assertRaises(AutostartError):
                target_for_entry(entry)
        with tempfile.TemporaryDirectory() as temporary:
            shell = Path(temporary) / "launch.cmd"
            shell.write_text("@echo fixture", encoding="utf-8")
            with self.assertRaises(AutostartError) as caught:
                target_for_entry(shell)
            self.assertEqual(caught.exception.code, "invalid_target")


class AutostartActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_toggle_and_refresh_follow_actual_registry_state(self):
        from PyQt5.QtWidgets import QMenu
        from aipet.ui.autostart import create_autostart_action
        registry = FakeRegistry()
        manager = AutostartManager(StartupTarget((r"C:\Fixture\宁宁.exe",)), registry=registry, windows=True)
        menu = QMenu()
        action = create_autostart_action(menu, manager=manager)
        self.assertFalse(action.isChecked())
        action.setChecked(True)
        self.assertEqual(manager.status(), "enabled")
        action.setChecked(False)
        self.assertEqual(manager.status(), "disabled")
        registry.key_exists = True
        registry.values[VALUE_NAME] = ("foreign.exe", 1)
        action.refresh_status()
        self.assertFalse(action.isEnabled())
        self.assertFalse(action.isChecked())
        self.assertEqual(registry.values[VALUE_NAME], ("foreign.exe", 1))
        menu.deleteLater()
