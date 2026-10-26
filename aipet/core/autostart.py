"""Opt-in current-user Windows sign-in startup; never requires elevation.

Only our exact named value is read or changed. A different command using the
same name is treated as a conflict, rather than adopted or overwritten.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "NenePetDesktop"


class AutostartError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _local_file(path):
    path = Path(path)
    if (not path.is_absolute() or ".." in path.parts
            or str(path).startswith(("\\\\", "//"))
            or any(char in str(path) for char in ('"', "\r", "\n", "\0"))):
        raise AutostartError("invalid_target")
    try:
        if not path.is_file():
            raise AutostartError("target_missing")
        for part in (path, *path.parents):
            info = part.lstat()
            if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise AutostartError("invalid_target")
    except OSError:
        raise AutostartError("target_missing") from None
    return path.resolve()


@dataclass(frozen=True)
class StartupTarget:
    arguments: tuple[str, ...]

    @property
    def command(self):
        # HKCU Run uses a Windows command line, not PowerShell or cmd.exe.
        command = subprocess.list2cmdline(self.arguments)
        if not command or len(command) > 260:
            raise AutostartError("command_too_long")
        return command


def target_for_entry(entry: Path, *, pythonw: Path | None = None) -> StartupTarget:
    entry = _local_file(entry)
    if entry.suffix.lower() == ".exe":
        return StartupTarget((str(entry),))
    if entry.suffix.lower() not in {".py", ".pyw"}:
        raise AutostartError("invalid_target")
    interpreter = _local_file(pythonw or Path(sys.executable).with_name("pythonw.exe"))
    if interpreter.name.lower() != "pythonw.exe":
        raise AutostartError("windowless_python_required")
    return StartupTarget((str(interpreter), str(entry)))


def default_target(root: Path | None = None) -> StartupTarget:
    if getattr(sys, "frozen", False):
        return target_for_entry(Path(sys.executable))
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    # The installed launcher also starts the user's existing Viewer. The
    # public portable entry does the same using its own first-run settings.
    installed = root / "宁宁桌宠-原版交互.exe"
    if installed.is_file():
        return target_for_entry(installed)
    return target_for_entry(root / "packaging/share_main.py")


class AutostartManager:
    def __init__(self, target: StartupTarget, *, registry=None, windows=None):
        self.target = target
        self._command = target.command
        self._registry = registry
        self.supported = os.name == "nt" if windows is None else bool(windows)

    def _api(self):
        if not self.supported:
            raise AutostartError("unsupported")
        if self._registry is None:
            import winreg
            self._registry = winreg
        return self._registry

    def _read(self, api, key):
        try:
            value, kind = api.QueryValueEx(key, VALUE_NAME)
        except FileNotFoundError:
            return "disabled"
        return "enabled" if kind == api.REG_SZ and value == self._command else "conflict"

    def status(self):
        if not self.supported:
            return "unsupported"
        api = self._api()
        try:
            with api.OpenKey(api.HKEY_CURRENT_USER, RUN_KEY, 0, api.KEY_READ) as key:
                return self._read(api, key)
        except FileNotFoundError:
            return "disabled"
        except OSError:
            raise AutostartError("registry_unavailable") from None

    def enable(self):
        api = self._api()
        try:
            with api.CreateKeyEx(api.HKEY_CURRENT_USER, RUN_KEY, 0,
                                 api.KEY_QUERY_VALUE | api.KEY_SET_VALUE) as key:
                state = self._read(api, key)
                if state == "conflict":
                    raise AutostartError("foreign_entry")
                if state == "enabled":
                    return False
                api.SetValueEx(key, VALUE_NAME, 0, api.REG_SZ, self._command)
                return True
        except OSError:
            raise AutostartError("registry_unavailable") from None

    def disable(self):
        api = self._api()
        try:
            with api.OpenKey(api.HKEY_CURRENT_USER, RUN_KEY, 0,
                             api.KEY_QUERY_VALUE | api.KEY_SET_VALUE) as key:
                state = self._read(api, key)
                if state == "conflict":
                    raise AutostartError("foreign_entry")
                if state == "disabled":
                    return False
                api.DeleteValue(key, VALUE_NAME)
                return True
        except FileNotFoundError:
            return False
        except OSError:
            raise AutostartError("registry_unavailable") from None
