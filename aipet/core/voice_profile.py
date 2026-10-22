"""Explicit local character voices, independent of the Murasame preset.

Paths in a JSON manifest are relative to the manifest. Only the engine's
known, matching pretrained GPT/SoVITS pairs are accepted; character LoRAs
must never leak into a zero-shot profile.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path


PROFILE_BASE_URL = "http://127.0.0.1:9881/tts"
BASE_MODELS = {
    "v1": (
        "s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt", "s2G488k.pth",
    ),
    "v2": (
        "gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt",
        "gsv-v2final-pretrained/s2G2333k.pth",
    ),
    "v3": ("s1v3.ckpt", "s2Gv3.pth"),
    "v4": ("s1v3.ckpt", "gsv-v4-pretrained/s2Gv4.pth"),
    "v2Pro": ("s1v3.ckpt", "v2Pro/s2Gv2Pro.pth"),
    "v2ProPlus": ("s1v3.ckpt", "v2Pro/s2Gv2ProPlus.pth"),
}
LANGUAGES = {
    "ja", "all_ja", "zh", "all_zh", "en", "auto", "yue", "all_yue",
    "ko", "all_ko", "auto_yue",
}
EMOTIONS = {"平静", "高兴", "害羞", "生气", "惊讶", "着急"}


class VoiceProfileError(ValueError):
    """A profile is incomplete or unsafe to use as a local zero-shot voice."""


@dataclass(frozen=True)
class VoiceReference:
    audio: Path
    transcript: str
    duration_seconds: float


@dataclass(frozen=True)
class VoiceProfile:
    manifest_path: Path
    engine_root: Path
    version: str
    gpt_weight: Path
    sovits_weight: Path
    references: dict[str, VoiceReference]
    text_lang: str
    prompt_lang: str
    device: str
    is_half: bool
    signature: str
    baseline_reference: VoiceReference | None = None

    def reference(self, emotion: str) -> VoiceReference:
        if not isinstance(emotion, str) or emotion not in EMOTIONS:
            raise VoiceProfileError("未知情绪标签: " + str(emotion))
        if emotion not in self.references and self.baseline_reference is not None:
            return self.baseline_reference
        try:
            return self.references[emotion]
        except KeyError as exc:
            raise VoiceProfileError(
                f"语音配置缺少情绪参考语音: {emotion} ({self.manifest_path})"
            ) from exc

    def engine_config(self) -> dict:
        pretrained = self.engine_root / "GPT_SoVITS" / "pretrained_models"
        return {"custom": {
            "version": self.version,
            "device": self.device,
            "is_half": self.is_half,
            "t2s_weights_path": str(self.gpt_weight),
            "vits_weights_path": str(self.sovits_weight),
            "bert_base_path": str(pretrained / "chinese-roberta-wwm-ext-large"),
            "cnhuhbert_base_path": str(pretrained / "chinese-hubert-base"),
        }}


def voice_profile_path(settings) -> str:
    # Keep the saved GPT profile available when switching back, but never let
    # its references or reserved port leak into the independent VITS service.
    if getattr(settings, "backend", "local") == "emotional_vits":
        return ""
    return str(getattr(settings, "voice_profile", "") or "").strip()


def tts_base_url(settings) -> str:
    if getattr(settings, "backend", "local") == "emotional_vits":
        # Its owned helper binds an ephemeral loopback port after model load.
        return ""
    return PROFILE_BASE_URL if voice_profile_path(settings) else settings.base_url


def load_voice_profile(path: str, engine_root: Path) -> VoiceProfile:
    manifest = Path(path).expanduser().resolve()
    engine_root = engine_root.resolve()
    try:
        raw = manifest.read_bytes()
        payload = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise VoiceProfileError(f"无法读取语音配置 {manifest}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
    ):
        raise VoiceProfileError("语音配置必须是 schema_version=1 的 JSON 对象")
    version = payload.get("version")
    if not isinstance(version, str) or version not in BASE_MODELS:
        raise VoiceProfileError(f"不支持的通用基座版本: {version!r}")
    pretrained = engine_root / "GPT_SoVITS" / "pretrained_models"
    weights = []
    for key, relative in zip(("gpt_weights", "sovits_weights"), BASE_MODELS[version]):
        selected = _manifest_file(payload.get(key), manifest.parent, key)
        expected = (pretrained / relative).resolve()
        if selected != expected:
            raise VoiceProfileError(
                f"{key} 必须使用 {version} 通用基座 {expected}；"
                "不能继承丛雨权重或角色 LoRA"
            )
        weights.append(selected)
    with weights[1].open("rb") as stream:
        if stream.read(2) in {b"03", b"04"}:
            raise VoiceProfileError("语音配置不能使用角色 SoVITS LoRA")
    text_lang = payload.get("text_lang", "ja")
    prompt_lang = payload.get("prompt_lang", "ja")
    for key, value in (("text_lang", text_lang), ("prompt_lang", prompt_lang)):
        if not isinstance(value, str) or value not in LANGUAGES:
            raise VoiceProfileError(f"不支持的 {key}: {value!r}")
        if version == "v1" and value in {"yue", "all_yue", "ko", "all_ko", "auto_yue"}:
            raise VoiceProfileError(f"v1 不支持 {key}={value}")
    device = payload.get("device", "cuda")
    if not isinstance(device, str) or device not in {"cpu", "cuda"}:
        raise VoiceProfileError("device 必须为 cpu 或 cuda")
    is_half = payload.get("is_half", device == "cuda")
    if not isinstance(is_half, bool) or (device == "cpu" and is_half):
        raise VoiceProfileError("is_half 必须是布尔值，CPU 模式必须为 false")
    entries = payload.get("references", {})
    baseline_entry = payload.get("baseline_reference")
    if not isinstance(entries, dict) or (not entries and baseline_entry is None):
        raise VoiceProfileError("语音配置必须提供非空 references 对象，或显式 baseline_reference")
    references = {}
    for emotion, entry in entries.items():
        if emotion not in EMOTIONS or not isinstance(entry, dict):
            raise VoiceProfileError(f"无效的情绪参考配置: {emotion}")
        references[emotion] = _load_reference(entry, manifest.parent, engine_root, emotion)
    baseline = (
        _load_reference(baseline_entry, manifest.parent, engine_root, "baseline_reference")
        if baseline_entry is not None else None
    )
    signature = hashlib.sha256(raw + str(engine_root).encode("utf-8")).hexdigest()
    return VoiceProfile(
        manifest, engine_root, version, weights[0], weights[1], references,
        text_lang, prompt_lang, device, is_half, signature, baseline,
    )


def _load_reference(entry, manifest_root: Path, engine_root: Path, label: str) -> VoiceReference:
    """Apply the same audio/transcript checks to explicit and baseline voices."""
    if not isinstance(entry, dict):
        raise VoiceProfileError("无效的参考配置: " + label)
    audio = _manifest_file(entry.get("audio"), manifest_root, label + ".audio")
    if audio.suffix.lower() not in {".wav", ".mp3", ".flac"}:
        raise VoiceProfileError(f"不支持的参考音频格式: {audio}")
    transcript_path = _manifest_file(entry.get("transcript"), manifest_root, label + ".transcript")
    try:
        transcript = transcript_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise VoiceProfileError(f"无法读取参考文本: {transcript_path}") from exc
    if not transcript:
        raise VoiceProfileError(f"参考文本为空: {transcript_path}")
    duration = _audio_duration(audio, engine_root)
    if not math.isfinite(duration) or not 3.0 <= duration <= 10.0:
        raise VoiceProfileError(f"参考音频必须为 3–10 秒: {audio} ({duration:.3f} 秒)")
    return VoiceReference(audio, transcript, duration)


def write_profile_engine_config(profile: VoiceProfile, cache_root: Path) -> Path:
    """JSON is valid YAML. Never read or copy the engine's custom YAML."""
    destination = cache_root / "voice-profiles" / profile.signature / "tts_infer.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(profile.engine_config(), ensure_ascii=False, indent=2) + "\n"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(destination)
    return destination.resolve()


def _manifest_file(value, root: Path, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise VoiceProfileError(f"语音配置缺少文件字段: {field}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        exists = path.is_file() and path.stat().st_size > 0
    except OSError as exc:
        raise VoiceProfileError(f"无法检查语音配置文件 ({field}): {path}") from exc
    if not exists:
        raise VoiceProfileError(f"语音配置文件不存在或为空 ({field}): {path}")
    return path


def _audio_duration(audio: Path, engine_root: Path) -> float:
    try:
        if audio.suffix.lower() == ".wav":
            with wave.open(str(audio), "rb") as stream:
                rate = stream.getframerate()
                if rate:
                    frames = stream.getnframes()
                    duration = frames / rate
                    if 3.0 <= duration <= 10.0:
                        expected_bytes = frames * stream.getnchannels() * stream.getsampwidth()
                        if len(stream.readframes(frames)) != expected_bytes:
                            raise VoiceProfileError(f"参考 WAV 文件不完整: {audio}")
                    return duration
    except (OSError, EOFError, wave.Error):
        pass
    probe = engine_root / "runtime" / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
    executable = str(probe) if probe.is_file() else shutil.which("ffprobe")
    if not executable:
        raise VoiceProfileError(f"无法校验参考音频长度，缺少 ffprobe: {audio}")
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(audio)],
            check=True, capture_output=True, text=True, encoding="utf-8",
            timeout=15, creationflags=0x08000000 if os.name == "nt" else 0,
        )
        return float(json.loads(result.stdout)["format"]["duration"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        raise VoiceProfileError(f"无法校验参考音频长度: {audio}") from exc
