"""Build a clean friend distribution without reading the production profile.

Run from a build Python with PyInstaller on PYTHONPATH. All copies are from an
explicit allowlist. Existing output directories are rejected rather than reset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

REPO = Path(__file__).resolve().parents[1]
MODEL_FILES = (
    "nene.model3.json", "nene.moc3", "nene.physics3.json", "nene.cdi3.json",
    "textures/texture_00.png", "motions/idle.motion3.json", "motions/tap.motion3.json",
)
EXCLUDES = (
    "torch", "torchaudio", "torchvision", "faster_whisper", "ctranslate2",
    "onnxruntime", "tensorflow", "scipy", "matplotlib", "pandas", "IPython",
    "jupyter", "notebook", "pytest", "tkinter", "PyQt6", "PySide6", "PySide2",
    "numba", "llvmlite", "transformers", "sentencepiece", "huggingface_hub",
)


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy(relative: str, destination: Path) -> None:
    source = (REPO / relative).resolve()
    if not source.is_relative_to(REPO) or not source.is_file():
        raise ValueError(f"Not an allowed source file: {relative}")
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def create_source(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for path in sorted((REPO / "aipet").rglob("*.py")):
        copy(path.relative_to(REPO).as_posix(), destination)
    for relative in (
        "LICENSE", "UPSTREAM_COMMIT.txt", "requirements.txt", "requirements-reading.txt",
        "requirements-voice.txt", "prompt.txt", "prompt.en.txt", "prompt.nene.txt",
        "思源黑体Bold.otf", "packaging/share_main.py", "packaging/share_support.py",
        "packaging/share_voice_check.py",
        "packaging/build_share.py", "packaging/collect_share_licenses.py",
        "packaging/build_voice_bundle.py",
        "README.md", "docs/LOCAL_ASSETS.md", ".gitignore",
        "tests/__init__.py", "tests/test_share_support.py",
    ):
        copy(relative, destination)
    for path in sorted((REPO / "assets/motions").glob("*.motion3.json")):
        copy(path.relative_to(REPO).as_posix(), destination)
    for name in MODEL_FILES:
        copy("assets/models/nene-editable/" + name, destination)
    for extension in ("wav", "txt"):
        copy("voice-sources/c2-2-nene-01-clause." + extension, destination)
    catalog = json.loads((REPO / "assets/persona/nene-scenes.json").read_text(encoding="utf-8"))
    # Keep verified evidence, remove capture-session details from metadata.
    catalog.pop("acceptance_cases", None)
    catalog.pop("excluded_sources", None)
    for scene in catalog["scenes"]:
        for item in scene["sources"]:
            copy(item["screenshot"], destination)
            metadata = json.loads((REPO / item["metadata"]).read_text(encoding="utf-8"))
            cleaned = {key: metadata[key] for key in ("id", "speaker", "speaker_label", "subtitle_zh", "status", "scene", "chapter") if key in metadata}
            dump(destination / item["metadata"], cleaned)
            item["metadata_sha256"] = hashlib.sha256((destination / item["metadata"]).read_bytes()).hexdigest()
    dump(destination / "assets/persona/nene-scenes.json", catalog)
    (destination / "BUILD.md").write_text(
        "# Corresponding source / 对应源码\n\n"
        "This is the source used for this Windows build. Upstream AIpet-Murasame "
        "is AGPL-3.0; see LICENSE and UPSTREAM_COMMIT.txt.\n\n"
        "Changes: Nene persona, Live2DViewerEX renderer bridge, Japanese external "
        "GPT-SoVITS speech, interaction/relationship controls, optional explicit "
        "screen reading, document tools, and a portable first-run launcher.\n\n"
        "Build with Windows x64 Python 3.13, requirements.txt, pynput, Pillow, "
        "sounddevice, pypdf==6.21.2 and PyInstaller==6.22.3. STT and engine torch "
        "are excluded from the core. Install PyInstaller into a private target "
        "and set PYTHONPATH to it. Run packaging/build_share.py --output NEW_DIR "
        "--build. It uses an explicit resource allowlist. The source checkout "
        "needs the same files under assets/ and voice-sources/ shown here.\n\n"
        "Runtime data are created beside the executable under data/. Never share "
        "your used data folder or API credentials. The Live2D model and reference "
        "audio have separate rights; AGPL does not license those assets.\n",
        encoding="utf-8",
    )


def build(source: Path, output: Path) -> None:
    resources = output / "resources"
    resources.mkdir(exist_ok=False)
    for relative in ("assets", "voice-sources"):
        shutil.copytree(source / relative, resources / relative)
    for relative in ("prompt.txt", "prompt.en.txt", "prompt.nene.txt", "思源黑体Bold.otf"):
        shutil.copy2(source / relative, resources / relative)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--windowed", "--noupx", "--name", "NenePet", "--contents-directory", "_internal",
        "--distpath", str(output / "dist"), "--workpath", str(output / "work"),
        "--specpath", str(output), "--paths", str(source), "--paths", str(source / "packaging"),
        "--collect-submodules", "pypdf", "--hidden-import", "pypdf",
        "--hidden-import", "pynput.keyboard._win32", "--hidden-import", "pynput.mouse._win32",
        "--hidden-import", "aipet.platforms.windows.runtime", "--hidden-import", "PyQt5.QtWebSockets",
        "--add-data", str(resources) + ":.",
    ]
    vendor = REPO / "vendor/reading"
    if (vendor / "pypdf").is_dir():
        command.extend(("--paths", str(vendor)))
    else:
        import importlib.util
        if importlib.util.find_spec("pypdf") is None:
            raise RuntimeError("Install requirements-reading.txt into the build Python before freezing.")
    for module in EXCLUDES:
        command.extend(("--exclude-module", module))
    command.append(str(source / "packaging/share_main.py"))
    dump(output / "build-command.json", command)
    subprocess.run(command, cwd=source, check=True)


def voice_profiles(destination: Path) -> None:
    for device in ("cpu", "cuda"):
        dump(destination / "voices" / f"nene-{device}.json", {
            "schema_version": 1, "character": "綾地寧々", "version": "v2Pro",
            "status": "baseline_voice_emotions_not_separately_calibrated",
            "gpt_weights": "../engine/GPT-SoVITS/GPT_SoVITS/pretrained_models/s1v3.ckpt",
            "sovits_weights": "../engine/GPT-SoVITS/GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
            "text_lang": "ja", "prompt_lang": "ja", "device": device,
            "is_half": device == "cuda", "references": {},
            "baseline_reference": {
                "audio": "../_internal/voice-sources/c2-2-nene-01-clause.wav",
                "transcript": "../_internal/voice-sources/c2-2-nene-01-clause.txt",
            },
        })


def archive_tree(folder: Path, target: Path, *, prefix: str | None = None) -> None:
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    done, last = 0, time.monotonic()
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
        for path in files:
            relative = path.relative_to(folder).as_posix()
            archive.write(path, (prefix + "/" if prefix else "") + relative)
            done += path.stat().st_size
            if time.monotonic() - last > 25:
                print(f"ZIP {done}/{total} bytes, {relative}", flush=True)
                last = time.monotonic()
    print(f"ZIP complete: {target.name}, {target.stat().st_size} bytes", flush=True)


def manifest(folder: Path) -> None:
    files = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name != "FILE-MANIFEST.json":
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files.append({"path": path.relative_to(folder).as_posix(), "bytes": path.stat().st_size, "sha256": digest})
    dump(folder / "FILE-MANIFEST.json", {"format": 1, "files": files})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    create_source(source)
    if args.build:
        build(source, output)
        package = output / "dist/NenePet"
        voice_profiles(package)
        archive_tree(source, package / "对应源码.zip", prefix="source")
    print(str(output), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
