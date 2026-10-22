"""Portable, secret-free bootstrap helpers; importing this module has no effects.

The resource directory belongs to the bundle; mutable settings belong beside
the executable. No helper reads the developer's existing pet profile.
"""
from __future__ import annotations

import importlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing, contextmanager
from typing import Iterable


_EXTERNAL_LAUNCH_LOCK = threading.RLock()


def external_environment(environment: dict[str, str] | None = None,
                         bundle_root: Path | None = None) -> dict[str, str]:
    """Uninherit frozen Python/Qt search paths, retaining an engine runtime PATH."""
    cleaned = dict(os.environ if environment is None else environment)
    root = bundle_root or (Path(sys._MEIPASS) if getattr(sys, "_MEIPASS", None) else None)
    if root is not None:
        prefix = os.path.normcase(os.path.abspath(str(root))).rstrip("\\/")
        kept = []
        for entry in cleaned.get("PATH", "").split(os.pathsep):
            path = os.path.normcase(os.path.abspath(entry.strip().strip('"'))) if entry.strip() else ""
            if path and (path == prefix or path.startswith(prefix + os.sep)):
                continue
            kept.append(entry)
        cleaned["PATH"] = os.pathsep.join(kept)
    removed = {"PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
               "QML2_IMPORT_PATH", "QML_IMPORT_PATH", "_MEIPASS2", "PYINSTALLER_RESET_ENVIRONMENT"}
    for key in list(cleaned):
        if key.upper() in removed or key.upper().startswith("_PYI_"):
            cleaned.pop(key)
    return cleaned


def _dll_directory_api():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    kernel.GetDllDirectoryW.restype = wintypes.DWORD
    kernel.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    kernel.SetDllDirectoryW.restype = wintypes.BOOL
    return kernel


@contextmanager
def external_dll_search_scope():
    """Temporarily undo PyInstaller's process-global Windows DLL directory."""
    with _EXTERNAL_LAUNCH_LOCK:
        if os.name != "nt" or not getattr(sys, "frozen", False):
            yield
            return
        import ctypes
        kernel = _dll_directory_api()
        previous = ctypes.create_unicode_buffer(32768)
        ctypes.set_last_error(0)
        length = kernel.GetDllDirectoryW(len(previous), previous)
        error = ctypes.get_last_error()
        if length >= len(previous) or (length == 0 and error):
            raise OSError(error, "无法保存 Windows DLL 搜索目录")
        if not kernel.SetDllDirectoryW(None):
            raise OSError(ctypes.get_last_error(), "无法隔离外部程序 DLL 搜索目录")
        try:
            yield
        finally:
            if not kernel.SetDllDirectoryW(previous.value or None):
                raise OSError(ctypes.get_last_error(), "无法恢复桌宠 DLL 搜索目录")


def safe_external_popen(*arguments, **options):
    """Launch a separate installed program, not another instance of this EXE."""
    options["env"] = external_environment(options.get("env"))
    owned = None
    try:
        with external_dll_search_scope():
            owned = subprocess.Popen(*arguments, **options)
        return owned
    except BaseException:
        # Restoring the parent's DLL directory can fail after creation. Do not
        # lose a live child before its caller has received/bound the handle.
        if owned is not None:
            try:
                if owned.poll() is None:
                    owned.terminate()
                    try:
                        owned.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        owned.kill()
                        owned.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise


class ExternalSubprocessProxy:
    """Module-local proxy: change only Popen, leave exceptions/constants intact."""
    def __init__(self, original=subprocess):
        self._original = original

    def __getattr__(self, name):
        return getattr(self._original, name)

    def Popen(self, *arguments, **options):
        return safe_external_popen(*arguments, **options)


def portable_roots() -> tuple[Path, Path]:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent, Path(sys._MEIPASS).resolve()
    project = Path(__file__).resolve().parents[1]
    # Running this entry as source also uses its own profile, never data/profile.
    return project / "data" / "share-development", project


def configure_environment(base: Path, resources: Path, *, create: bool = False) -> Path:
    base, resources = base.resolve(), resources.resolve()
    profile = base / "data" / "profile"
    if create:
        profile.mkdir(parents=True, exist_ok=True)
        (base / "data" / "cache").mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "AIPET_APPLICATION_NAME": "宁宁桌宠 · 分享版",
        "AIPET_DATA_DIR": str(profile),
        "AIPET_CONFIG_PATH": str(profile / "config.json"),
        "AIPET_CACHE_DIR": str(base / "data" / "cache"),
        "AIPET_MODEL_DIR": str(resources / "models"),
        "AIPET_RENDERER": "viewerex",
        "AIPET_CHARACTER_NAME": "宁宁",
        "AIPET_VIEWER_SLOT": "1",
        "AIPET_VIEWER_MODEL_PATH": str(resources / "assets" / "models" / "nene-editable" / "nene.model3.json"),
        # Never inherit the developer's Viewer coordinates from the environment.
        "AIPET_VIEWER_BBOX": "", "AIPET_VIEWER_POSITION": "",
        "AIPET_VIEWER_MANAGED_POSITION": "0", "AIPET_VIEWER_DEBUG_BBOX": "0",
        "AIPET_VIEWER_PORT_FILE": str(profile / "viewer-not-configured"),
        "AIPET_VIEWER_PORT": "10086",
    })
    return profile


def fresh_settings(base: Path, resources: Path):
    """Explicit fresh friend profile. Never copy or load installed settings."""
    from aipet.core.config import AppSettings
    return AppSettings.model_validate({
        "ui_language": "zh-CN", "mode": "api",
        "api": {"provider": "deepseek", "deepseek_api_key": "", "aliyun_api_key": "",
                "openai_api_key": "", "deepseek_chat_model": "deepseek-v4-flash",
                "deepseek_thinking": False},
        "character": {"profile_id": "nene", "name": "宁宁", "name_en": "Nene",
                      "user_name": "你", "portrait": "b", "outfit": "uniform",
                      "personality_file": "prompt.nene.txt", "personality_file_en": "prompt.nene.txt"},
        "tts": {"enabled": False, "backend": "local", "advanced_paths": True,
                "engine_root": str(base / "engine" / "GPT-SoVITS"),
                "model_dir": str(base / "voices"), "storage_root": str(base / "engine"),
                "voice_profile": "", "base_url": "http://127.0.0.1:9881/tts"},
        "stt": {"enabled": False, "device": "cpu", "language": "zh",
                "model_dir": str(base / "data" / "models" / "whisper" / "large-v3")},
        "vision": {"enabled": False}, "display": {"show_log_console": False},
        "idle": {"do_not_disturb": False},
    })


def apply_voice_choice(settings, base: Path, selected_voice: str) -> None:
    """Apply the simple setup's local CPU/CUDA selection after file validation."""
    if not selected_voice:
        settings.tts.enabled = False
        return
    from aipet.core.voice_profile import load_voice_profile
    engine = base / "engine" / "GPT-SoVITS"
    load_voice_profile(selected_voice, engine)  # Files only, no inference.
    settings.tts.enabled = True
    settings.tts.backend = "local"
    settings.tts.base_url = "http://127.0.0.1:9881/tts"
    settings.tts.advanced_paths = True
    settings.tts.engine_root = str(engine)
    settings.tts.model_dir = str(base / "voices")
    settings.tts.voice_profile = selected_voice


def steam_roots() -> list[Path]:
    """Read only known Steam installation registry values, never account data."""
    try:
        import winreg
    except ImportError:
        return []
    roots = []
    for hive, key, name in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, name)
            if isinstance(value, str) and value.strip():
                roots.append(Path(value))
        except OSError:
            continue
    return roots


def library_paths(vdf_text: str) -> list[Path]:
    """Handle both the current object and old numeric-string VDF formats."""
    # Steam writes paths as escaped quoted strings. Avoid unicode_escape: it
    # corrupts non-ASCII Windows library directory names.
    tokens = re.findall(r'"((?:\\.|[^"\\])*)"|([{}])', vdf_text)
    quoted = [re.sub(r'\\([\\"])', r'\1', value) if not brace else brace
              for value, brace in tokens]
    result = []
    depth, index = 0, 0
    while index < len(quoted):
        token = quoted[index]
        if token == "}":
            depth -= 1
            index += 1
            continue
        if index + 1 >= len(quoted):
            break
        following = quoted[index + 1]
        if following == "{":
            depth += 1
            index += 2
            continue
        # Consume key/value pairs together: a numeric value is not another key.
        # Nested numeric app-size mappings must not become directory paths.
        if token == "path" or (depth == 1 and token.isdigit()):
            if following not in {"{", "}"} and following.strip():
                result.append(Path(following))
        index += 2
    return result


def normalize_viewer_root(path: Path) -> Path | None:
    path = path.expanduser().resolve()
    if path.is_file():
        path = path.parent
    if path.name.lower() == "bin":
        path = path.parent
    if (path / "launcher.exe").is_file() and (path / "bin").is_dir():
        return path
    return None


def discover_viewers(base: Path, roots: Iterable[Path] | None = None) -> list[Path]:
    candidates = [base / "Live2DViewerEX", base / "viewer" / "Live2DViewerEX"]
    for root in steam_roots() if roots is None else roots:
        libraries = [root]
        try:
            text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8-sig")
            libraries.extend(library_paths(text))
        except (OSError, UnicodeError):
            pass
        candidates.extend(library / "steamapps" / "common" / "Live2DViewerEX" for library in libraries)
    found = []
    for candidate in candidates:
        verified = normalize_viewer_root(candidate)
        if verified is not None and verified not in found:
            found.append(verified)
    return found


def parse_coordinates(value: str, count: int) -> tuple[float, ...] | None:
    if not value.strip():
        return None
    numbers = tuple(float(item.strip()) for item in value.split(","))
    if len(numbers) != count or not all(math.isfinite(item) for item in numbers):
        raise ValueError("坐标数量不正确，或含非有限数字")
    if count == 4 and (numbers[2] < 40 or numbers[3] < 80):
        raise ValueError("人物范围的宽度至少 40，高度至少 80")
    return numbers


def read_viewer_settings(profile: Path) -> dict:
    path = profile / "share-viewer.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("分享版 Viewer 配置格式无效，请重新运行首次设置")
    return value


def apply_viewer_settings(value: dict) -> Path:
    configured = value.get("viewer_root", "")
    if not isinstance(configured, str) or not configured:
        raise ValueError("尚未选择 Live2DViewerEX 安装目录")
    root = normalize_viewer_root(Path(configured))
    if root is None:
        raise ValueError("Live2DViewerEX 目录不包含 launcher.exe 和 bin，请重新选择")
    bbox = parse_coordinates(str(value.get("bbox") or ""), 4)
    position = parse_coordinates(str(value.get("position") or ""), 2)
    if value.get("managed_position") is True and position is None:
        raise ValueError("受控位置需要已校准的 ExAPI X,Y 坐标")
    os.environ.update({
        "AIPET_VIEWER_PORT_FILE": str(root / "bin" / "app_port"),
        "AIPET_VIEWER_BBOX": "" if bbox is None else ",".join(str(round(number)) for number in bbox),
        "AIPET_VIEWER_POSITION": "" if position is None else ",".join(str(number) for number in position),
        "AIPET_VIEWER_MANAGED_POSITION": "1" if value.get("managed_position") is True else "0",
    })
    return root


def write_viewer_settings(profile: Path, value: dict) -> None:
    # Validate the complete new value before touching an existing file.
    apply_viewer_settings(value)
    profile.mkdir(parents=True, exist_ok=True)
    temporary = profile / "share-viewer.json.tmp"
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(profile / "share-viewer.json")


def hidden_process_options() -> dict:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def interactive_process_options() -> dict:
    """Hide a console without hiding the setup child's first real Qt window."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def launch_viewer(root: Path) -> bool:
    """Ask an installed launcher to start; never install/update/login to Steam."""
    normalized = normalize_viewer_root(root)
    if normalized is None:
        raise ValueError("未找到已安装的 Live2DViewerEX")
    # A port file can be stale; the Qt renderer reconnects and reports that.
    # Avoid re-running the launcher when this Viewer is already discoverable.
    try:
        port = int((normalized / "bin" / "app_port").read_text(encoding="utf-8-sig").strip())
        import socket
        if 1 <= port <= 65535:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    return False
            except OSError:
                pass
    except (OSError, ValueError):
        pass
    safe_external_popen([str(normalized / "launcher.exe")], cwd=str(normalized),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **hidden_process_options())
    return True


def self_test(base: Path, resources: Path) -> dict:
    """Offline resources/imports only: no profile reads, writes, model or UI start."""
    checks = {}
    errors = []
    for relative in ("prompt.nene.txt", "assets/persona/nene-scenes.json",
                     "assets/models/nene-editable/nene.model3.json"):
        path = resources / relative
        good = path.is_file() and path.stat().st_size > 0
        checks[relative] = good
        if not good:
            errors.append("missing_resource:" + relative)
    # Runtime imports verify PyInstaller's collected graph. The entry's static
    # application import and build's hidden-import list perform collection.
    for name in ("aipet.core.config", "aipet.core.backends", "aipet.core.storage",
                 "aipet.ui.audio_playback", "aipet.ui.live2d_renderer",
                 "aipet.ui.utility_assistant", "aipet.application",
                 "PyQt5.QtMultimedia", "PyQt5.QtWebSockets",
                 "pynput.keyboard._win32", "pynput.mouse._win32"):
        try:
            importlib.import_module(name)
            checks["import:" + name] = True
        except Exception as exc:
            checks["import:" + name] = False
            errors.append("import_failed:" + name + ":" + type(exc).__name__ + ":" + str(exc)[:500])
    try:
        from aipet.ui.live2d_renderer import validate_model3_path
        validate_model3_path(resources / "assets/models/nene-editable/nene.model3.json")
        checks["model_dependencies"] = True
    except Exception as exc:
        checks["model_dependencies"] = False
        errors.append("model_dependencies:" + type(exc).__name__ + ":" + str(exc)[:500])
    try:
        from aipet.core.nene_persona import verify_scene_sources
        scenes = verify_scene_sources()
        checks["persona_all_sources_match"] = scenes["all_sources_match"]
        checks["persona_screenshot_count"] = scenes["unique_screenshots"]
        if not scenes["all_sources_match"] or scenes["unique_screenshots"] != 12:
            errors.append("persona_evidence_incomplete:expected_12_verified_screenshots")
    except Exception as exc:
        errors.append("persona_evidence:" + type(exc).__name__ + ":" + str(exc)[:500])
    try:
        from aipet.core.reading_materials import _load_pypdf
        pdf = _load_pypdf()
        writer = pdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        temporary_pdf = io.BytesIO()
        writer.write(temporary_pdf)
        temporary_pdf.seek(0)
        reader = pdf.PdfReader(temporary_pdf)
        if len(reader.pages) != 1 or reader.pages[0].extract_text() != "":
            raise ValueError("synthetic PDF roundtrip failed")
        checks["pdf_dynamic_import_and_memory_roundtrip"] = True
        checks["pypdf_version"] = str(pdf.__version__)
    except Exception as exc:
        checks["pdf_dynamic_import_and_memory_roundtrip"] = False
        errors.append("pdf:" + type(exc).__name__ + ":" + str(exc)[:500])
    try:
        from PyQt5.QtMultimedia import QAudio, QAudioDeviceInfo
        devices = QAudioDeviceInfo.availableDevices(QAudio.AudioOutput)
        checks["audio_output_enumeration"] = True
        checks["audio_output_device_count"] = len(devices)
        checks["default_audio_output_present"] = not QAudioDeviceInfo.defaultOutputDevice().isNull()
        # No output device is not a broken text-only bundle. Audio compatibility
        # is a separate machine-specific result and no sound is played here.
    except Exception as exc:
        checks["audio_output_enumeration"] = False
        errors.append("audio_enumeration:" + type(exc).__name__ + ":" + str(exc)[:500])
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("SELECT 1").fetchone()
        checks["sqlite_in_memory"] = True
    except Exception as exc:
        errors.append("sqlite:" + type(exc).__name__)
    try:
        external_runtime = str(resources.resolve().parent / "external-runtime")
        example = {"PATH": os.pathsep.join((str(resources), str(resources / "PyQt5"), external_runtime)),
                   "PYTHONHOME": "bundle-python", "PYTHONPATH": "bundle-imports",
                   "QT_PLUGIN_PATH": "bundle-plugins", "_PYI_APPLICATION_HOME_DIR": "bundle"}
        isolated = external_environment(example, resources)
        if isolated.get("PATH") != external_runtime or any(name in isolated for name in example if name != "PATH"):
            raise ValueError("external process environment remains contaminated")
        with external_dll_search_scope():
            pass  # Check save/reset/restore without launching an external program.
        checks["external_process_environment_isolated"] = True
        checks["windows_dll_isolation_exercised"] = os.name == "nt" and bool(getattr(sys, "frozen", False))
    except Exception as exc:
        checks["external_process_environment_isolated"] = False
        errors.append("external_process_isolation:" + type(exc).__name__ + ":" + str(exc)[:500])
    return {"schema_version": 1, "success": not errors, "scope": "offline_bundle_only",
            "profile_read": False, "profile_written": False, "windows_opened": False,
            "network_used": False, "resources": str(resources), "portable_base": str(base),
            "checks": checks, "errors": errors,
            "not_verified": ["Viewer 显示与点击", "好友 API 账号及余额", "音色与情绪", "硬件兼容性"]}
