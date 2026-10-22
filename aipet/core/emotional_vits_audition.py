"""Local, bounded audition metadata and explicit experimental profile export.

This module uses only the standard library and the lightweight profile reader.
It never loads a model, starts a service, plays audio or changes saved settings.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import uuid
import wave

from aipet.core.emotional_vits_profile import (
    EMOTIONS, EmotionalVITSReferenceVector, load_emotional_vits_profile,
    verify_reference_vector,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / "voices/nene-emotion-auditions.json"
# Pin the reviewed catalog independently of its embedded WAV/vector digests.
CATALOG_SHA256 = "c0b64abb7730df25b75bddab3a30b7b5780e1730b5f5014bfb2b1fabb2ba0065"
MAX_AUDIO_BYTES = 2 * 1024 * 1024
LEGACY_REPLAY_UNKNOWN = frozenset({"c3-3-nene-01", "c3-3-nene-02"})
CANDIDATE_IDS = ("author-preset-0434", "c2-5-nene-03", "c2-5-nene-04", "c2-5-nene-06",
                 "c3-3-nene-01", "c3-3-nene-02", "c3-3-nene-03", "c3-3-nene-04",
                 "c3-3-nene-05-retake-01")


class AuditionError(ValueError):
    pass


class AuditionCancelled(AuditionError):
    pass


@dataclass(frozen=True)
class AuditionCandidate:
    identifier: str
    audio: Path
    audio_sha256: str
    duration_seconds: float
    bank_id: int | None
    vector: EmotionalVITSReferenceVector | None
    source_audit: Mapping
    source_wave: Path | None = None
    source_wave_sha256: str | None = None


@dataclass(frozen=True)
class AuditionCatalog:
    path: Path
    sha256: str
    text: str
    seed: int
    base_profile: object
    candidates: tuple[AuditionCandidate, ...]


def _check_cancelled(cancelled) -> None:
    if cancelled is not None and cancelled():
        raise AuditionCancelled("Audition operation cancelled")


def _read(path: Path, maximum: int, cancelled=None) -> bytes:
    _check_cancelled(cancelled)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise AuditionError("Audition file exceeds the bounded size: " + path.name)
    _check_cancelled(cancelled)
    return raw


def _digest(raw: bytes, expected: str, name: str) -> None:
    if not isinstance(expected, str) or hashlib.sha256(raw).hexdigest() != expected:
        raise AuditionError("Audition SHA256 mismatch: " + name)


def _within(value, parent: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise AuditionError("Missing local audition path")
    path = (parent / value).resolve()
    try:
        path.relative_to(parent.resolve())
    except ValueError as exc:
        raise AuditionError("Audition path is outside its local source directory") from exc
    if not path.is_file():
        raise AuditionError("Audition file is missing: " + str(path))
    return path


def _audit(value: dict, identifier: str) -> dict:
    if not isinstance(value, dict):
        raise AuditionError("Missing source audit: " + identifier)
    for key in ("ordinary_reference_pool_eligible", "formal_reference_eligible", "promoted",
                "source_truncated_possible"):
        if value.get(key) is not False:
            raise AuditionError("Source cannot be silently approved: " + identifier)
    if value.get("emotion_confirmed", False) is not False:
        raise AuditionError("Source emotion is not confirmed: " + identifier)
    warnings = value.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise AuditionError("Missing source warnings: " + identifier)
    replay = value.get("replay_window_truncated_possible")
    if identifier in LEGACY_REPLAY_UNKNOWN:
        if replay is not None or "legacy_replay_window_check_not_recorded" not in warnings:
            raise AuditionError("Legacy replay-window uncertainty must be preserved")
    elif replay is not False:
        raise AuditionError("Source replay-window check is missing")
    return deepcopy(value)


def load_audition_catalog(path=DEFAULT_CATALOG, *, root=ROOT,
                          expected_sha256=CATALOG_SHA256, cancelled=None) -> AuditionCatalog:
    """Validate the fixed local catalog in a worker, without NumPy/Torch imports."""
    root = Path(root).resolve()
    manifest = Path(path).resolve()
    raw = _read(manifest, 128 * 1024, cancelled)
    _digest(raw, expected_sha256, manifest.name)
    payload = json.loads(raw.decode("utf-8"))
    if (payload.get("schema_version") != 1 or payload.get("kind") != "nene-emotion-auditions"
            or payload.get("status") != "candidate_listening_pending"):
        raise AuditionError("Unsupported audition catalog")
    experiment = root / "data/experiments/nene-emotion-extractor"
    report_entry = payload["transfer_report"]
    report_path = _within(report_entry["path"], experiment)
    report_raw = _read(report_path, 256 * 1024, cancelled)
    _digest(report_raw, report_entry["sha256"], report_path.name)
    report = json.loads(report_raw.decode("utf-8"))
    if (report.get("status") != "generated_requires_listening" or report.get("scope_passed") is not True
            or report.get("protected_files_unchanged") is not True or report.get("cleanup_error")
            or report.get("formal_emotion_approval") is not False
            or report.get("route_label_is_technical_only") is not True):
        raise AuditionError("Comparison is incomplete or its experimental scope changed")
    base_entry = payload["base_profile"]
    base_path = _within(base_entry["path"], experiment)
    _digest(_read(base_path, 65536, cancelled), base_entry["sha256"], base_path.name)
    base = load_emotional_vits_profile(base_path)
    if base.device != "cpu" or base.status != "experimental":
        raise AuditionError("Audition base must remain an experimental CPU profile")
    if payload.get("text") != report.get("text") or payload.get("seed") != report.get("seed"):
        raise AuditionError("Audition comparison text or seed changed")
    rows = payload.get("candidates")
    if not isinstance(rows, list) or tuple(row.get("id") for row in rows) != CANDIDATE_IDS:
        raise AuditionError("Expected the fixed nine audition candidates")
    variants = {row["id"]: row for row in report.get("variants", [])}
    if set(variants) != set(CANDIDATE_IDS):
        raise AuditionError("Comparison does not contain all nine candidates")
    candidates = []
    for row in rows:
        _check_cancelled(cancelled)
        identifier = row["id"]
        variant = variants[identifier]
        audio = _within(row["audio"], experiment)
        _digest(_read(audio, MAX_AUDIO_BYTES, cancelled), row["audio_sha256"], audio.name)
        if (audio.suffix.lower() != ".wav" or row["audio_sha256"] != variant["wav_sha256"]
                or Path(variant["audio"]["path"]).resolve() != audio
                or variant["audio"].get("passed") is not True):
            raise AuditionError("Audition does not match the compared WAV")
        with wave.open(str(audio), "rb") as stream:
            frames, rate = stream.getnframes(), stream.getframerate()
            if (stream.getnchannels() != 1 or stream.getsampwidth() != 2 or rate != 22050
                    or stream.getcomptype() != "NONE" or not frames
                    or len(stream.readframes(frames)) != frames * 2):
                raise AuditionError("Expected a complete mono PCM16/22050Hz audition")
            duration = frames / rate
        if not 0 < duration <= 15:
            raise AuditionError("Audition is not a short preview")
        condition_path = _within(str(Path(variant["profile"]).resolve()), experiment)
        condition = load_emotional_vits_profile(condition_path)
        vector = None
        source_wave = None
        source_sha = None
        if identifier == "author-preset-0434":
            if row.get("bank_id") != 434 or row.get("vector") is not None or dict(condition.emotion_ids) != {"平静": 434}:
                raise AuditionError("Author candidate must retain bank ID 434")
            bank_id, audit = 434, {}
        else:
            if row.get("bank_id") is not None or condition.emotion_ids:
                raise AuditionError("Reference candidate cannot become a bank ID")
            entry = row["vector"]
            vector = EmotionalVITSReferenceVector(_within(entry["path"], experiment), entry["sha256"])
            if condition.emotion_vectors.get("平静") != vector:
                raise AuditionError("Reference candidate differs from the compared vector")
            verify_reference_vector(vector)
            audit = _audit(row.get("source_audit"), identifier)
            if audit != variant.get("source_audit"):
                raise AuditionError("Source audit differs from the comparison")
            source_wave = _within(row["source_wave"], root / "voice-sources")
            if source_wave.name != identifier + ".wav":
                raise AuditionError("Unexpected source recording")
            source_sha = row["source_wave_sha256"]
            _digest(_read(source_wave, 8 * 1024 * 1024, cancelled), source_sha, source_wave.name)
            bank_id = None
        candidates.append(AuditionCandidate(identifier, audio, row["audio_sha256"], duration,
                                            bank_id, vector, MappingProxyType(audit), source_wave, source_sha))
    return AuditionCatalog(manifest, expected_sha256, payload["text"], payload["seed"], base, tuple(candidates))


def export_audition_profile(catalog: AuditionCatalog, selections: Mapping[str, str], parent,
                            *, cancelled=None) -> Path:
    """Create a new self-contained vector bundle; never overwrite an existing file."""
    if not selections or any(label not in EMOTIONS for label in selections):
        raise AuditionError("Select at least one of the six known emotion labels")
    indexed = {item.identifier: item for item in catalog.candidates}
    if any(value not in indexed for value in selections.values()):
        raise AuditionError("Selection is outside the reviewed candidate catalog")
    _check_cancelled(cancelled)
    base = catalog.base_profile
    payload = {"schema_version": 1, "kind": "nene-emotional-vits", "status": "experimental",
               "experiment_root": str(base.experiment_root), "runtime_executable": str(base.runtime_executable),
               "device": "cpu", "threads": base.threads, "speaker_id": base.speaker_id,
               "emotion_ids": {}, "emotion_vectors": {}, "route_semantics_approved": False,
               "audition_selection": {"catalog": str(catalog.path), "catalog_sha256": catalog.sha256,
                                      "text": catalog.text, "seed": catalog.seed,
                                      "selection_is_experimental": True, "emotions": {}},
               "notes": ["Explicit user-selected experimental routes; playback or selection is not six-emotion approval.",
                         "Unconfigured emotions fail explicitly. Original bank, references and manifests are unchanged."]}
    snapshots = {}
    for label, identifier in selections.items():
        _check_cancelled(cancelled)
        candidate = indexed[identifier]
        metadata = {"candidate_id": identifier, "preview_wave": str(candidate.audio),
                    "preview_wave_sha256": candidate.audio_sha256, "emotion_confirmed": False}
        if candidate.vector is None:
            payload["emotion_ids"][label] = candidate.bank_id
        else:
            audit = _audit(dict(candidate.source_audit), identifier)
            snapshots[identifier] = verify_reference_vector(candidate.vector)
            payload["emotion_vectors"][label] = {"path": "vectors/" + identifier + ".npy",
                                                  "sha256": candidate.vector.sha256}
            metadata.update(source_audit=audit, source_wave=str(candidate.source_wave),
                            source_wave_sha256=candidate.source_wave_sha256)
        payload["audition_selection"]["emotions"][label] = metadata
    destination = Path(parent).expanduser().resolve()
    if not destination.is_dir():
        raise AuditionError("Choose an existing directory for the new profile")
    bundle = destination / ("nene-emotion-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    bundle.mkdir(exist_ok=False)
    created = []
    vectors_dir = bundle / "vectors"
    try:
        if snapshots:
            vectors_dir.mkdir()
        for identifier, raw in snapshots.items():
            _check_cancelled(cancelled)
            target = vectors_dir / (identifier + ".npy")
            with target.open("xb") as stream:
                created.append(target)
                stream.write(raw)
        _check_cancelled(cancelled)
        profile_path = bundle / "profile.json"
        with profile_path.open("x", encoding="utf-8") as stream:
            created.append(profile_path)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        loaded = load_emotional_vits_profile(profile_path)
        if loaded.configured_emotions() != frozenset(selections):
            raise AuditionError("Exported profile does not preserve the selected routes")
        _check_cancelled(cancelled)
        return profile_path
    except BaseException:
        # Only files created by this invocation are eligible for cleanup.
        for target in reversed(created):
            target.unlink(missing_ok=True)
        if vectors_dir.exists():
            vectors_dir.rmdir()
        bundle.rmdir()
        raise
