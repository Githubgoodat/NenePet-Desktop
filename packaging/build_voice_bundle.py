"""Build an optional, fresh GPT-SoVITS v2Pro Windows NVIDIA extension.

Default/--dry-run is read-only. --stage copies a fixed inference allowlist to a
NEW stage directory; --zip archives only a completed, hash-verified stage. No
source file is changed, no training/personal state is included intentionally,
and no Python/model/desktop application from the engine is executed.

Examples (run with the build machine's Python, not the engine's Python 3.9)::

    python packaging/build_voice_bundle.py --dry-run
    python packaging/build_voice_bundle.py --stage
    python packaging/build_voice_bundle.py --zip build/NenePet-Voice-NVIDIA.zip
    python packaging/build_voice_bundle.py --stage --zip output.zip

The extension contains engine/GPT-SoVITS, not a pet executable, API credentials,
user settings/history, voice-cloning reference, or a voice-profile selection.
The core release must separately supply its approved reference and relocatable
voice profile. An installed NVIDIA driver is needed for CUDA; actual synthesis
must be verified on the target hardware. The audit below checks selected text
and names, not the provenance or hidden contents of third-party binary models.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import time
import zipfile


DEFAULT_SOURCE = Path("local-assets") / "GPT-SoVITS"
DEFAULT_STAGE = Path("build") / "voice-stage"
ARCHIVE_PREFIX = "engine/GPT-SoVITS"
MANIFEST_NAME = "VOICE-BUNDLE-MANIFEST.json"
CHUNK_BYTES = 8 * 1024 * 1024
PROGRESS_SECONDS = 30.0

# Complete packaged runtime is retained, including OpenJTalk's dictionary,
# Torch/CUDA DLLs, FFmpeg, NLTK data, and dependency license/metadata files.
# Engine source trees are explicit; training checkpoints/output are not roots.
TREE_ALLOWLIST = (
    "runtime",
    "GPT_SoVITS/AR", "GPT_SoVITS/BigVGAN", "GPT_SoVITS/eres2net",
    "GPT_SoVITS/f5_tts", "GPT_SoVITS/feature_extractor",
    "GPT_SoVITS/module", "GPT_SoVITS/text", "GPT_SoVITS/TTS_infer_pack",
    "GPT_SoVITS/pretrained_models/chinese-hubert-base",
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
    "GPT_SoVITS/pretrained_models/fast_langdetect",
    "GPT_SoVITS/pretrained_models/sv",
    # audio_sr is imported by TTS.py even when super_sampling is False.
    "tools/AP_BWE_main", "tools/i18n",
)
FILE_ALLOWLIST = frozenset({
    "api_v2.py", "LICENSE", "README.md", "requirements.txt",
    "GPT_SoVITS/process_ckpt.py", "GPT_SoVITS/sv.py", "GPT_SoVITS/utils.py",
    "GPT_SoVITS/pretrained_models/s1v3.ckpt",
    "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
    "tools/__init__.py", "tools/audio_sr.py", "tools/my_utils.py",
})
REQUIRED_FILES = (
    "api_v2.py", "LICENSE", "runtime/python.exe", "runtime/python39.dll",
    "runtime/python39.zip", "runtime/python39._pth", "runtime/ffmpeg.exe",
    "runtime/ffprobe.exe", "runtime/LICENSE.txt",
    "GPT_SoVITS/TTS_infer_pack/TTS.py",
    "GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py",
    "GPT_SoVITS/module/models.py", "GPT_SoVITS/f5_tts/model/__init__.py",
    "GPT_SoVITS/eres2net/ERes2NetV2.py", "GPT_SoVITS/sv.py",
    "GPT_SoVITS/pretrained_models/s1v3.ckpt",
    "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
    "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt",
    "GPT_SoVITS/pretrained_models/chinese-hubert-base/config.json",
    "GPT_SoVITS/pretrained_models/chinese-hubert-base/preprocessor_config.json",
    "GPT_SoVITS/pretrained_models/chinese-hubert-base/pytorch_model.bin",
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/config.json",
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/tokenizer.json",
    "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large/pytorch_model.bin",
    "GPT_SoVITS/pretrained_models/fast_langdetect/lid.176.bin",
    "GPT_SoVITS/text/japanese.py", "GPT_SoVITS/text/ja_userdic/userdict.csv",
    "GPT_SoVITS/text/ja_userdic/user.dict", "GPT_SoVITS/text/ja_userdic/userdict.md5",
    "GPT_SoVITS/text/G2PWModel/g2pW.onnx",
    "runtime/Lib/site-packages/pyopenjtalk/__init__.py",
    "runtime/Lib/site-packages/pyopenjtalk/open_jtalk_dic_utf_8-1.11/sys.dic",
    "tools/i18n/i18n.py", "tools/i18n/locale/en_US.json",
    "tools/audio_sr.py", "tools/AP_BWE_main/models/model.py",
    "tools/AP_BWE_main/datasets1/dataset.py", "tools/AP_BWE_main/LICENSE",
    "GPT_SoVITS/BigVGAN/LICENSE",
)
PERSONAL_NAMES = frozenset({
    ".env", ".pypirc", "pip.ini", "pip.conf", ".netrc", ".git-credentials",
    ".bash_history", ".python_history", ".npmrc", ".condarc",
    "token", "token.txt", "credentials", "client_secret.json", "service-account.json",
    "credentials.json", "history.json", "continuity.json", "conversation-memory.json",
    "conversation_memory.json", "screen_memory.json", "history.sqlite", "history.sqlite-wal", "history.sqlite-shm",
    "config.local.json", "known_hosts", "id_rsa", "id_ed25519", "direct_url.json",
})
PERSONAL_PARTS = frozenset({
    ".git", ".ssh", ".aws", ".azure", ".config", ".cache", ".ipynb_checkpoints",
    ".ipython", ".jupyter", ".huggingface", ".codex", ".ollama",
    "__pycache__", "~_pycache__", "logs", "output", "temp", "tensorboard", "wandb",
})
TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".json", ".yaml", ".yml", ".txt", ".md", ".rst",
    ".ini", ".cfg", ".csv", ".pth", "._pth", ".toml", ".xml", ".html",
})
# Only high-specificity indications are reported; never print matched contents.
PRIVATE_PATTERNS = (
    ("developer_home_path", re.compile(rb'''(?i)[a-z]:[/\\\\]+users[/\\\\]+[^/\\\s"']+(?:[/\\\\]|\b)''')),
    ("openai_or_deepseek_key_assignment", re.compile(
        rb'''(?i)(?:api[_-]?key|authorization|bearer)[^\r\n]{0,24}["']?sk-[A-Za-z0-9_-]{28,}''')),
    # A parser's PEM header constant is not a private key. Require two payload
    # lines, rather than blocking cryptography/google-auth's format handling.
    ("private_key_material", re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[ \t]*(?:\r?\n|\\n)"
        rb"(?:[ \t]*[A-Za-z0-9+/=]{32,}(?:\r?\n|\\n)){2}")),
)


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    relative: str
    size: int
    mtime_ns: int


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _reparse(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def checked_child(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise BuildError("Unsafe relative path")
    child = root.joinpath(*rel.parts)
    if not child.resolve().is_relative_to(root):
        raise BuildError("Path escapes its declared root")
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.exists() and _reparse(cursor):
            raise BuildError("Reparse/symlink is not permitted: " + relative)
    return child


def excluded_reason(relative: str) -> str | None:
    parts = PurePosixPath(relative).parts
    folded = tuple(part.casefold() for part in parts)
    name = folded[-1]
    if relative.startswith("runtime/Lib/site-packages/Crypto/SelfTest/"):
        return "upstream_crypto_selftest_private_key_fixtures"
    if any(part in PERSONAL_PARTS for part in folded):
        return "cache_log_output_or_user_directory"
    if name in PERSONAL_NAMES or name.startswith(".env."):
        return "credential_or_personal_state_filename"
    if name.endswith((".log", ".tmp", ".bak", ".dmp", ".dump")):
        return "log_temporary_or_backup_file"
    if name == "g2pwmodel_1.1.zip":
        return "redundant_frontend_download_archive"
    # Public CA bundles are needed; upstream test private-key fixtures are not.
    if name.endswith((".key", ".pem", ".p12", ".pfx")) and name not in {"cacert.pem", "roots.pem"}:
        return "key_or_non_ca_certificate_file"
    return None


def allowed(relative: str) -> bool:
    return relative in FILE_ALLOWLIST or any(
        relative.startswith(prefix + "/") for prefix in TREE_ALLOWLIST)


def clean_config() -> bytes:
    # Never copy the source tts_infer.yaml: it is mutable and holds local paths.
    fields = {
        "device": "cuda", "is_half": True, "version": "v2Pro",
        "t2s_weights_path": "GPT_SoVITS/pretrained_models/s1v3.ckpt",
        "vits_weights_path": "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth",
        "bert_base_path": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
        "cnhuhbert_base_path": "GPT_SoVITS/pretrained_models/chinese-hubert-base",
    }
    # JSON is a YAML subset and avoids another dependency in this build tool.
    return (json.dumps({"custom": fields, "v2Pro": fields}, indent=2) + "\n").encode("utf-8")


def plan(source: Path) -> tuple[list[Entry], dict]:
    if not source.is_dir() or _reparse(source):
        raise BuildError("Source must be an existing, non-reparse directory")
    entries: list[Entry] = []
    excluded_counts: Counter = Counter()
    excluded_bytes: Counter = Counter()
    groups = defaultdict(lambda: {"files": 0, "bytes": 0})
    last_progress = time.monotonic()
    for directory, directories, files in os.walk(source, followlinks=False):
        parent = Path(directory)
        for name in list(directories):
            child = parent / name
            if _reparse(child):
                relative = child.relative_to(source).as_posix()
                if allowed(relative + "/placeholder"):
                    raise BuildError("Reparse directory in allowlist: " + relative)
                directories.remove(name)
                excluded_counts["reparse_directory_not_followed"] += 1
        for name in files:
            child = parent / name
            relative = child.relative_to(source).as_posix()
            info = child.lstat()
            if not stat.S_ISREG(info.st_mode) or _reparse(child):
                if allowed(relative):
                    raise BuildError("Non-regular file in allowlist: " + relative)
                excluded_counts["non_regular_file"] += 1
                continue
            reason = excluded_reason(relative)
            if not allowed(relative):
                reason = reason or "outside_inference_allowlist"
            if reason:
                excluded_counts[reason] += 1
                excluded_bytes[reason] += info.st_size
                continue
            entries.append(Entry(relative, info.st_size, info.st_mtime_ns))
            group = "runtime" if relative.startswith("runtime/") else "engine_inference_resources"
            groups[group]["files"] += 1
            groups[group]["bytes"] += info.st_size
            if time.monotonic() - last_progress >= PROGRESS_SECONDS:
                emit({"event": "progress", "operation": "plan", "selected_files": len(entries)})
                last_progress = time.monotonic()
    entries.sort(key=lambda entry: entry.relative)
    names = {entry.relative for entry in entries}
    missing = [name for name in REQUIRED_FILES if name not in names]
    if missing:
        raise BuildError("Required inference resources missing: " + ", ".join(missing))
    runtime_pth = checked_child(source, "runtime/python39._pth").read_text(encoding="utf-8-sig")
    for raw in runtime_pth.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and line != "import site":
            if re.match(r"^[A-Za-z]:", line) or line.startswith(("/", "\\")) or ".." in PurePosixPath(line.replace("\\", "/")).parts:
                raise BuildError("Runtime python39._pth contains an external path; inspect before sharing")
    return entries, {
        "files": len(entries) + 1,
        "bytes": sum(entry.size for entry in entries) + len(clean_config()),
        "generated_files": ["GPT_SoVITS/configs/tts_infer.yaml"],
        "groups": dict(groups),
        "excluded": {reason: {"files": count, "bytes": excluded_bytes[reason]}
                     for reason, count in sorted(excluded_counts.items())},
        "excluded_roots": ["logs", "output", "TEMP", "GPT_weights*", "SoVITS_weights*",
                           "other_pretrained_versions", "source_mutable_tts_infer.yaml"],
    }


def audit(source: Path, entries: list[Entry]) -> dict:
    findings = []
    scanned_files = scanned_bytes = 0
    selected = []
    for entry in entries:
        rel = PurePosixPath(entry.relative)
        if rel.suffix.casefold() not in TEXT_SUFFIXES and not rel.name.casefold().startswith(("license", "copying", "notice")):
            continue
        # Avoid treating the huge binary model format .pth as a text file.
        if rel.suffix.casefold() == ".pth" and entry.relative.startswith("GPT_SoVITS/pretrained_models/"):
            continue
        selected.append(entry)
    progress = Progress("privacy_audit", sum(entry.size for entry in selected), len(selected))
    for entry in selected:
        path = checked_child(source, entry.relative)
        found = set()
        overlap = b""
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                scanned_bytes += len(block)
                progress.update(size=len(block))
                window = overlap + block
                for category, pattern in PRIVATE_PATTERNS:
                    if pattern.search(window):
                        found.add(category)
                overlap = window[-512:]
        scanned_files += 1
        progress.update(completed=True)
        findings.extend({"path": entry.relative, "category": category} for category in sorted(found))
    progress.update(force=True)
    return {"selected_text_files_scanned": scanned_files, "selected_text_bytes_scanned": scanned_bytes,
            "findings": findings,
            "scope": "selected text and filename checks; opaque models, DLLs and compiled code are not privacy-certified"}


class Progress:
    def __init__(self, operation: str, total_bytes: int, total_files: int):
        self.operation, self.total_bytes, self.total_files = operation, total_bytes, total_files
        self.started = self.last = time.monotonic()
        self.bytes = self.files = 0

    def update(self, *, size: int = 0, completed: bool = False, force: bool = False) -> None:
        self.bytes += size
        self.files += int(completed)
        now = time.monotonic()
        if force or now - self.last >= PROGRESS_SECONDS:
            emit({"event": "progress", "operation": self.operation,
                  "files_done": self.files, "files_total": self.total_files,
                  "bytes_done": self.bytes, "bytes_total": self.total_bytes,
                  "elapsed_seconds": round(now - self.started, 1)})
            self.last = now


def validate_destination(source: Path, destination: Path) -> None:
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise BuildError("Destination and source trees must be disjoint")
    cursor = destination
    while not cursor.exists() and cursor != cursor.parent:
        cursor = cursor.parent
    if cursor.exists() and _reparse(cursor):
        raise BuildError("Destination parent must not be a reparse directory")


def stage(source: Path, destination: Path, entries: list[Entry], privacy: dict) -> Path:
    validate_destination(source, destination)
    if destination.exists():
        raise BuildError("Stage already exists; choose a NEW --stage-root (nothing was removed)")
    total_bytes = sum(entry.size for entry in entries) + len(clean_config())
    ancestor = destination.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if shutil.disk_usage(ancestor).free < total_bytes + 512 * 1024 * 1024:
        raise BuildError("Insufficient free space for a complete stage plus 512 MiB margin")
    destination.mkdir(parents=True, exist_ok=False)
    engine = destination / ARCHIVE_PREFIX
    engine.mkdir(parents=True, exist_ok=False)
    records = []
    progress = Progress("stage", total_bytes, len(entries) + 1)
    for entry in entries:
        origin = checked_child(source, entry.relative)
        current = origin.stat()
        if current.st_size != entry.size or current.st_mtime_ns != entry.mtime_ns:
            raise BuildError("Source changed after planning: " + entry.relative)
        target = checked_child(engine, entry.relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        copied = 0
        with origin.open("rb") as incoming, target.open("xb") as outgoing:
            while block := incoming.read(CHUNK_BYTES):
                outgoing.write(block)
                digest.update(block)
                copied += len(block)
                progress.update(size=len(block))
        final = origin.stat()
        if copied != entry.size or final.st_size != entry.size or final.st_mtime_ns != entry.mtime_ns:
            raise BuildError("Source changed while copying: " + entry.relative)
        records.append({"path": f"{ARCHIVE_PREFIX}/{entry.relative}", "bytes": copied, "sha256": digest.hexdigest()})
        progress.update(completed=True)
    generated = clean_config()
    target = checked_child(engine, "GPT_SoVITS/configs/tts_infer.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(generated)
    records.append({"path": f"{ARCHIVE_PREFIX}/GPT_SoVITS/configs/tts_infer.yaml",
                    "bytes": len(generated), "sha256": hashlib.sha256(generated).hexdigest()})
    progress.update(size=len(generated), completed=True, force=True)
    manifest = {
        "schema_version": 1, "complete": True, "kind": "optional_gpt_sovits_engine",
        "platform": "windows-x64-nvidia", "model_version": "v2Pro",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "no_pet_profile_or_reference_included": True,
        "source_files_unchanged_by_builder": True,
        "privacy_audit": privacy, "files": records,
        "runtime_command": ["runtime/python.exe", "api_v2.py", "-a", "127.0.0.1", "-p", "9881",
                            "-c", "GPT_SoVITS/configs/tts_infer.yaml"],
        "runtime_cwd": ARCHIVE_PREFIX, "runtime_path_prefix": "runtime",
    }
    path = destination / MANIFEST_NAME
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    emit({"event": "stage_complete", "files": len(records), "bytes": total_bytes, "manifest": str(path)})
    return path


def archive_stage(stage_root: Path, output: Path) -> None:
    if output.exists():
        raise BuildError("ZIP already exists; choose a NEW output (nothing was removed)")
    if output.is_relative_to(stage_root):
        raise BuildError("ZIP must be outside the stage directory")
    manifest_path = checked_child(stage_root, MANIFEST_NAME)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("schema_version") != 1 or manifest.get("complete") is not True:
        raise BuildError("A complete stage manifest is required")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise BuildError("Manifest does not contain stage files")
    expected = {MANIFEST_NAME}
    for record in records:
        relative = record.get("path", "")
        if not relative.startswith(ARCHIVE_PREFIX + "/") or relative in expected:
            raise BuildError("Manifest has duplicate or out-of-scope paths")
        engine_relative = relative[len(ARCHIVE_PREFIX) + 1:]
        if engine_relative != "GPT_SoVITS/configs/tts_infer.yaml" and (
                not allowed(engine_relative) or excluded_reason(engine_relative)):
            raise BuildError("Manifest contains a file outside the release allowlist")
        child = checked_child(stage_root, relative)
        if not child.is_file() or child.stat().st_size != record.get("bytes"):
            raise BuildError("Staged file missing or changed: " + relative)
        expected.add(relative)
    actual = set()
    for directory, directories, files in os.walk(stage_root, followlinks=False):
        parent = Path(directory)
        for name in directories:
            if _reparse(parent / name):
                raise BuildError("Stage contains a reparse directory")
        for name in files:
            relative = (parent / name).relative_to(stage_root).as_posix()
            checked_child(stage_root, relative)
            actual.add(relative)
    if actual != expected:
        raise BuildError("Stage has extra or missing files; refusing to archive unreviewed content")
    total = sum(record["bytes"] for record in records)
    progress = Progress("zip", total, len(records))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves every previous release, even after failure.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=1, allowZip64=True, strict_timestamps=False) as archive:
        archive.writestr(MANIFEST_NAME, manifest_bytes)
        for record in records:
            relative = record["path"]
            path = checked_child(stage_root, relative)
            digest = hashlib.sha256()
            read_bytes = 0
            with path.open("rb") as incoming, archive.open(relative, "w", force_zip64=True) as outgoing:
                while block := incoming.read(CHUNK_BYTES):
                    outgoing.write(block)
                    digest.update(block)
                    read_bytes += len(block)
                    progress.update(size=len(block))
            if read_bytes != record["bytes"] or digest.hexdigest() != record["sha256"]:
                raise BuildError("Staged file hash changed; ZIP is incomplete: " + relative)
            progress.update(completed=True)
    progress.update(force=True)
    emit({"event": "zip_complete", "path": str(output), "archive_bytes": output.stat().st_size,
          "files": len(records) + 1, "zip64": True, "compresslevel": 1})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--stage-root", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--dry-run", action="store_true", help="read-only plan and selected-text audit; default")
    parser.add_argument("--stage", action="store_true", help="copy to a new stage directory")
    parser.add_argument("--zip", dest="zip_path", type=Path, metavar="NEW_ZIP", help="ZIP64 a completed stage; may combine with --stage")
    args = parser.parse_args(argv)
    if args.dry_run and (args.stage or args.zip_path):
        parser.error("--dry-run cannot be combined with --stage/--zip")
    source = args.source.expanduser().resolve()
    stage_root = args.stage_root.expanduser().resolve()
    try:
        if args.zip_path and not args.stage:
            output = args.zip_path.expanduser().resolve()
            validate_destination(source, output)
            archive_stage(stage_root, output)
            return 0
        entries, summary = plan(source)
        privacy = audit(source, entries)
        emit({"event": "dry_run", "read_only": not args.stage, "summary": summary,
              "privacy_audit": privacy,
              "limits": ["No reference voice or pet profile is included.",
                         "No engine process or model is started by this builder.",
                         "Binary packages and models retain their own licenses and are not content-audited."]})
        if privacy["findings"]:
            raise BuildError("Selected text has possible private content; inspect listed paths before staging")
        if args.stage:
            stage(source, stage_root, entries, privacy)
            if args.zip_path:
                output = args.zip_path.expanduser().resolve()
                validate_destination(source, output)
                archive_stage(stage_root, output)
        return 0
    except (BuildError, OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
        # Deliberately retain partial output for inspection; never delete files.
        emit({"event": "error", "error_type": type(exc).__name__, "detail": str(exc),
              "partial_output_if_any": "retained; do not distribute without stage_complete/zip_complete"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
