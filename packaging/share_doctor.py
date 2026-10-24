"""Read-only source readiness checks requiring only the Python standard library.

No application imports, configuration discovery, network or process launches.
Presence checks do not establish model compatibility or a working installation.
"""
from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
from pathlib import Path, PureWindowsPath
import struct
import sys


REQUIRED_DISTRIBUTIONS = {
    "numpy": ">=2.0,<3", "opencv-python": ">=4.10,<5",
    "pydantic": ">=2.11,<3", "PyQt5": ">=5.15.11,<6",
    "paramiko": ">=3.5,<5", "requests": ">=2.32,<3",
}
OPTIONAL_DISTRIBUTIONS = {
    "pypdf": "==6.21.2 (PDF text reading)",
    "pynput": ">=1.7,<2 (optional input hooks)",
    "Pillow": ">=12,<13 (optional image helpers)",
    "sounddevice": ">=0.5,<1 (optional audio device helpers)",
}
MODEL_LABEL = "assets/models/nene-editable/nene.model3.json"
_PROTECTED_PARTS = {
    "data", "profile", "profiles", "assets", "voice-sources", "voices",
    "engine", "models", "local-assets", ".git", ".venv",
}


def _nonempty_file(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root) and path.is_file() and path.stat().st_size > 0
    except (OSError, ValueError):
        return False


def _model_references(path: Path, root: Path) -> list[str]:
    """Check declared file references, without loading MOC or claiming Cubism validation."""
    issues: list[str] = []
    try:
        if not path.resolve().is_relative_to(root):
            return ["model_entry_outside_resources"]
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ["model_entry_unreadable_or_invalid_json"]
    if not isinstance(manifest, dict) or type(manifest.get("Version")) is not int or manifest["Version"] != 3:
        return ["model_entry_requires_version_3"]
    references = manifest.get("FileReferences")
    if not isinstance(references, dict):
        return ["model_entry_missing_file_references"]

    def require(value, label: str) -> None:
        # Reject absolute/escaping references before checking any external file.
        if (not isinstance(value, str) or not value.strip()
                or Path(value).is_absolute() or PureWindowsPath(value).anchor):
            issues.append(label + ":invalid_local_reference")
            return
        dependency = path.parent / value.replace("\\", "/")
        if not _nonempty_file(dependency, path.parent.resolve()):
            issues.append(label + ":missing_empty_or_outside_model_directory")

    require(references.get("Moc"), "Moc")
    textures = references.get("Textures")
    if not isinstance(textures, list) or not textures:
        issues.append("Textures:missing_or_invalid")
    else:
        for index, value in enumerate(textures):
            require(value, f"Textures[{index}]")
    for key in ("Physics", "Pose", "DisplayInfo", "UserData", "MotionSync", "ParameterController"):
        if key in references:
            require(references[key], key)
    expressions = references.get("Expressions", [])
    if not isinstance(expressions, list):
        issues.append("Expressions:invalid_list")
    else:
        for index, entry in enumerate(expressions):
            require(entry.get("File") if isinstance(entry, dict) else None, f"Expressions[{index}].File")
    motions = references.get("Motions", {})
    if not isinstance(motions, dict):
        issues.append("Motions:invalid_object")
    else:
        # Use sequence labels, never echo arbitrary model keys or local paths.
        for group_index, entries in enumerate(motions.values()):
            if not isinstance(entries, list):
                issues.append(f"Motions[{group_index}]:invalid_list")
                continue
            for index, entry in enumerate(entries):
                label = f"Motions[{group_index}][{index}]"
                require(entry.get("File") if isinstance(entry, dict) else None, label + ".File")
                if isinstance(entry, dict) and "Sound" in entry:
                    require(entry["Sound"], label + ".Sound")
    return issues


def diagnose(resources: Path, *, base: Path | None = None, viewer_root: Path | None = None) -> dict:
    """Return safe, path-free readiness facts; never inspect a user profile."""
    resources = resources.resolve()
    base = (base or resources / "data" / "share-development").resolve()
    checks: list[dict] = []

    def add(name: str, level: str, status: str, detail: str, **extra) -> None:
        checks.append({"name": name, "level": level, "status": status, "detail": detail, **extra})

    frozen = bool(getattr(sys, "frozen", False))
    add("source_mode", "required", "unsupported" if frozen else "ok",
        "--doctor 仅用于源码环境；冻结包请使用 --share-self-test，安装元数据不用于判断冻结包依赖。")
    add("platform", "required", "ok" if sys.platform == "win32" and struct.calcsize("P") == 8 else "unsupported",
        "公开分享入口面向 Windows 64 位；其他平台未验收。")
    add("python", "required", "ok" if sys.version_info[:2] == (3, 13) else "unsupported",
        "开发运行使用 Python 3.13；其他版本未验收。", version=f"{sys.version_info.major}.{sys.version_info.minor}")
    for level, distributions in (("required", REQUIRED_DISTRIBUTIONS), ("optional", OPTIONAL_DISTRIBUTIONS)):
        for name, expected in distributions.items():
            if frozen:
                continue
            try:
                version = metadata.version(name)
            except metadata.PackageNotFoundError:
                add("dependency:" + name, level, "missing", "未找到安装元数据。", expected=expected)
            except (OSError, ValueError):
                add("dependency:" + name, level, "unreadable", "安装元数据无法读取。", expected=expected)
            else:
                add("dependency:" + name, level, "present", "仅确认安装元数据存在；未导入模块或验证版本兼容与 DLL。",
                    version=version[:100], expected=expected)
    add("prompt.nene.txt", "required", "ok" if _nonempty_file(resources / "prompt.nene.txt", resources) else "missing",
        "宁宁预设提示词应存在且非空。")
    model = resources / MODEL_LABEL
    if not _nonempty_file(model, resources):
        add(MODEL_LABEL, "required", "missing", "请按 docs/LOCAL_ASSETS.md 准备有权使用的导出模型。")
    else:
        problems = _model_references(model, resources)
        add(MODEL_LABEL, "required", "invalid" if problems else "ok",
            "检查 JSON 和已声明的本地文件引用；不验证 MOC 内容、参数、点击事件或实际显示。", issues=problems)
    catalog = resources / "assets/persona/nene-scenes.json"
    add("scene_evidence", "optional", "present_unverified" if _nonempty_file(catalog, resources) else "missing",
        "缺少原作场景证据时使用提示词降级；存在目录也未校验截图或哈希。")
    add("voice", "optional", "present_unverified" if any(_nonempty_file(base / "voices" / name, base)
        for name in ("nene-cpu.json", "nene-cuda.json")) else "missing",
        "默认关闭 TTS；不读取语音配置、音频或引擎文件，也不证明音色和情绪可用。")
    if viewer_root is None:
        add("viewer", "optional", "not_checked", "未自动查找安装或读取设置；可显式传入 --viewer-root DIR 核对安装结构。")
    else:
        try:
            good = _nonempty_file(viewer_root / "launcher.exe", viewer_root.resolve()) and (viewer_root / "bin").is_dir()
        except (OSError, ValueError):
            good = False
        add("viewer", "required", "present_unverified" if good else "invalid",
            "仅检查显式目录中的 launcher.exe 与 bin；未检查授权、ExAPI、端口或显示状态。")
    blockers = [item["name"] for item in checks if item["level"] == "required"
                and item["status"] not in {"ok", "present", "present_unverified"}]
    return {
        "schema_version": 1, "success": not blockers, "scope": "source_readiness_only",
        "profile_read": False, "profile_written": False, "network_used": False,
        "viewer_started": False, "windows_opened": False,
        "checks": checks, "blockers": blockers,
        "not_verified": ["模块导入与版本兼容", "首次设置与 API 账号", "Viewer 授权、显示、点击与置顶",
                         "MOC 内容与参数兼容", "真实音频、音色、情绪与 GPU 兼容", "场景证据与哈希"],
    }


def write_report(report: dict, destination: Path | None = None) -> int:
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if destination is None:
        if sys.stdout is not None:
            print(encoded, end="")
    else:
        # Do not create directories or replace a report/state/resource file.
        raw = destination.expanduser().absolute()
        target = raw.resolve()
        if (raw.exists() or raw.is_symlink() or target.suffix.lower() != ".json"
                or any(part.casefold() in _PROTECTED_PARTS for part in (*raw.parts, *target.parts))
                or not target.parent.is_dir()):
            raise ValueError("报告必须是已有普通目录内的新 .json 文件，且不能位于配置、素材或引擎目录。")
        with target.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
    return 0 if report["success"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--viewer-root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report = diagnose(Path(__file__).resolve().parents[1], viewer_root=args.viewer_root)
    try:
        return write_report(report, args.report)
    except (ValueError, OSError):
        if sys.stderr is not None:
            print("无法写入诊断报告：请使用已有普通目录内的新 .json 文件，并避开配置、素材与引擎目录。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
