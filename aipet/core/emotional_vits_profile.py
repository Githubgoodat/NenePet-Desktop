"""Lightweight, explicit profiles for the isolated Nene Emotional VITS runtime.

Loading a profile never imports a model or validates large checkpoint contents.
An emotion mapping is a configuration choice, not evidence of listening approval.
Keep this module compatible with the model runtime's Python 3.9.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
from pathlib import Path
import re
import struct
from types import MappingProxyType
from typing import Mapping


PROFILE_KIND = "nene-emotional-vits"
EMOTIONS = frozenset({"平静", "高兴", "害羞", "生气", "惊讶", "着急"})
EMOTION_COUNT = 5458
MAX_VECTOR_BYTES = 128 * 1024
VECTOR_DIMENSIONS = 1024


class EmotionalVITSProfileError(ValueError):
    """A selected profile is invalid or lacks the requested emotion mapping."""


@dataclass(frozen=True)
class EmotionalVITSReferenceVector:
    path: Path
    sha256: str


@dataclass(frozen=True)
class EmotionalVITSProfile:
    manifest_path: Path
    experiment_root: Path
    runtime_executable: Path
    device: str
    threads: int
    speaker_id: int
    emotion_ids: Mapping[str, int]
    status: str
    signature: str
    emotion_vectors: Mapping[str, EmotionalVITSReferenceVector] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def configured_emotions(self) -> frozenset[str]:
        return frozenset(self.emotion_ids) | frozenset(self.emotion_vectors)

    def require_emotion(self, emotion: str) -> None:
        if not isinstance(emotion, str) or emotion not in EMOTIONS:
            raise EmotionalVITSProfileError("未知情绪标签: " + str(emotion))
        if emotion not in self.configured_emotions():
            raise EmotionalVITSProfileError("Emotional VITS 配置缺少情绪映射: " + emotion)

    def emotion_id(self, emotion: str) -> int:
        self.require_emotion(emotion)
        if emotion in self.emotion_vectors:
            raise EmotionalVITSProfileError("该情绪使用独立参考向量而非 bank ID: " + emotion)
        return self.emotion_ids[emotion]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EmotionalVITSProfileError("语音配置包含重复字段: " + key)
        result[key] = value
    return result


def _path(value, parent: Path, field: str, *, directory: bool) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise EmotionalVITSProfileError("语音配置缺少路径字段: " + field)
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = parent / path
    path = path.resolve()
    if not (path.is_dir() if directory else path.is_file()):
        raise EmotionalVITSProfileError("语音配置路径不存在: " + field + " = " + str(path))
    return path


def _vector_header(stream, size: int) -> int:
    """Inspect a bounded NPY header without importing NumPy or reading data."""
    if not 0 < size <= MAX_VECTOR_BYTES:
        raise EmotionalVITSProfileError("参考情绪向量必须为不超过 128 KiB 的 NPY 文件")
    prefix = stream.read(8)
    if len(prefix) != 8 or prefix[:6] != b"\x93NUMPY" or prefix[6:] not in {b"\x01\x00", b"\x02\x00", b"\x03\x00"}:
        raise EmotionalVITSProfileError("参考情绪向量的 NPY 格式不正确")
    length_size = 2 if prefix[6] == 1 else 4
    length_bytes = stream.read(length_size)
    if len(length_bytes) != length_size:
        raise EmotionalVITSProfileError("参考情绪向量的 NPY header 不完整")
    length = int.from_bytes(length_bytes, "little")
    if not 0 < length <= 8192:
        raise EmotionalVITSProfileError("参考情绪向量的 NPY header 过大或为空")
    raw_header = stream.read(length)
    try:
        if len(raw_header) != length or not raw_header.endswith(b"\n"):
            raise ValueError("incomplete header")
        header = ast.literal_eval(raw_header.decode("utf-8" if prefix[6] == 3 else "latin1").strip())
    except (ValueError, SyntaxError, UnicodeError, RecursionError) as exc:
        raise EmotionalVITSProfileError("参考情绪向量的 NPY header 不正确") from exc
    if (not isinstance(header, dict) or set(header) != {"descr", "fortran_order", "shape"}
            or header.get("descr") not in ("<f4", "=f4", "f4", "float32")
            or type(header.get("fortran_order")) is not bool
            or not isinstance(header.get("shape"), tuple) or len(header["shape"]) != 1
            or type(header["shape"][0]) is not int or header["shape"] != (VECTOR_DIMENSIONS,)):
        raise EmotionalVITSProfileError("参考情绪向量必须为 float32 shape(1024,)，禁止 object/pickle")
    offset = stream.tell()
    if size != offset + VECTOR_DIMENSIONS * 4:
        raise EmotionalVITSProfileError("参考情绪向量数据长度不正确")
    return offset


def verify_reference_vector(reference: EmotionalVITSReferenceVector) -> bytes:
    """Bounded stdlib content validation for offline diagnostics and helpers.

    Return the verified snapshot so NumPy loads the same bytes, even if the
    file changes later. Profile loading and Qt settings checks never call it.
    """
    try:
        with reference.path.open("rb") as stream:
            raw = stream.read(MAX_VECTOR_BYTES + 1)
    except OSError as exc:
        raise EmotionalVITSProfileError("无法读取参考情绪向量: " + str(reference.path)) from exc
    offset = _vector_header(io.BytesIO(raw), len(raw))
    if hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise EmotionalVITSProfileError("参考情绪向量 SHA256 不匹配: " + str(reference.path))
    if any(not math.isfinite(value[0]) for value in struct.iter_unpack("<f", raw[offset:])):
        raise EmotionalVITSProfileError("参考情绪向量包含非有限值: " + str(reference.path))
    return raw


def load_emotional_vits_profile(path) -> EmotionalVITSProfile:
    """Read only JSON and filesystem metadata; never load Torch or execute code."""
    manifest = Path(path).expanduser().resolve()
    try:
        if manifest.stat().st_size > 65536:
            raise EmotionalVITSProfileError("Emotional VITS profile 超过 64 KiB")
        raw = manifest.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EmotionalVITSProfileError("无法读取 Emotional VITS 配置: " + str(exc)) from exc
    if not isinstance(payload, dict):
        raise EmotionalVITSProfileError("Emotional VITS 配置必须为 JSON 对象")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise EmotionalVITSProfileError("不支持的 Emotional VITS schema_version")
    if payload.get("kind") != PROFILE_KIND:
        raise EmotionalVITSProfileError("不匹配的 Emotional VITS kind")
    if payload.get("device") != "cpu":
        raise EmotionalVITSProfileError("Emotional VITS 目前仅支持 device=cpu")
    threads = payload.get("threads")
    if type(threads) is not int or not 1 <= threads <= 4:
        raise EmotionalVITSProfileError("Emotional VITS threads 必须是 1–4 的整数")
    if type(payload.get("speaker_id")) is not int or payload["speaker_id"] != 0:
        raise EmotionalVITSProfileError("宁宁 Emotional VITS 必须使用 speaker_id=0")
    status = payload.get("status")
    if not isinstance(status, str) or status not in {"experimental", "verified"}:
        raise EmotionalVITSProfileError("Emotional VITS status 必须为 experimental 或 verified")
    emotions = payload.get("emotion_ids", {})
    if not isinstance(emotions, dict):
        raise EmotionalVITSProfileError("Emotional VITS emotion_ids 必须为对象（可部分配置）")
    for label, sample_id in emotions.items():
        if label not in EMOTIONS:
            raise EmotionalVITSProfileError("未知情绪标签: " + label)
        if type(sample_id) is not int or not 0 <= sample_id < EMOTION_COUNT:
            raise EmotionalVITSProfileError("情绪样本 ID 必须是 0–5457 的整数: " + label)
    references = payload.get("emotion_vectors", {})
    if not isinstance(references, dict):
        raise EmotionalVITSProfileError("Emotional VITS emotion_vectors 必须为对象（可部分配置）")
    vectors = {}
    for label, entry in references.items():
        if label not in EMOTIONS:
            raise EmotionalVITSProfileError("未知情绪标签: " + label)
        if label in emotions:
            raise EmotionalVITSProfileError("同一情绪不能同时配置 bank ID 和参考向量: " + label)
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise EmotionalVITSProfileError("参考情绪向量必须包含 path 和 sha256: " + label)
        digest = entry["sha256"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise EmotionalVITSProfileError("参考情绪向量 sha256 必须为 64 位十六进制: " + label)
        vector_path = _path(entry["path"], manifest.parent, "emotion_vectors." + label, directory=False)
        if vector_path.suffix.lower() != ".npy":
            raise EmotionalVITSProfileError("参考情绪向量必须为 .npy 文件: " + label)
        try:
            with vector_path.open("rb") as stream:
                _vector_header(stream, vector_path.stat().st_size)
        except OSError as exc:
            raise EmotionalVITSProfileError("无法读取参考情绪向量 header: " + label) from exc
        vectors[label] = EmotionalVITSReferenceVector(vector_path, digest.lower())
    experiment = _path(payload.get("experiment_root"), manifest.parent, "experiment_root", directory=True)
    executable = _path(payload.get("runtime_executable"), manifest.parent, "runtime_executable", directory=False)
    paths = {"manifest_path": str(manifest), "experiment_root": str(experiment),
             "runtime_executable": str(executable)}
    if vectors:
        paths["emotion_vectors"] = {label: {"path": str(vector.path), "sha256": vector.sha256}
                                    for label, vector in vectors.items()}
    resolved = json.dumps(paths, ensure_ascii=False, sort_keys=True).encode("utf-8")
    signature = hashlib.sha256(raw + b"\0" + resolved).hexdigest()
    return EmotionalVITSProfile(manifest, experiment, executable, "cpu", threads, 0,
                                MappingProxyType(dict(emotions)), status, signature,
                                MappingProxyType(vectors))
