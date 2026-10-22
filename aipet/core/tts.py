"""Shared text-to-speech client."""

from __future__ import annotations

import time
import threading
import uuid
from pathlib import Path
from typing import Callable

import requests

from aipet.core.backends import Emotion
from aipet.core.config import AppSettings, get_cache_dir
from aipet.core.network import is_loopback_url
from aipet.core.runtime_logging import get_logger
from aipet.core.tts_assets import configure_local_tts_weights, locate_tts_assets
from aipet.core.tts_service import TTSServiceError, get_tts_service_manager
from aipet.core.voice_profile import VoiceProfileError, tts_base_url, voice_profile_path


logger = get_logger("tts")


class TTSError(RuntimeError):
    pass


class TTSCancelled(TTSError):
    """The caller no longer owns this synthesis request."""


class TTSClient:
    def __init__(self, settings: AppSettings, *, cancelled: Callable[[], bool] | None = None):
        self.settings = settings
        self._cancelled = threading.Event()
        self._external_cancelled = cancelled
        self._closed = False
        self.session = requests.Session()
        if settings.tts.backend == "emotional_vits" or is_loopback_url(tts_base_url(settings.tts)):
            self.session.trust_env = False
        self._weights_configured = None

    def cancel(self) -> None:
        # Session.close does not interrupt a blocked HTTP request. Do not close
        # a session from another thread or stop a service shared by newer work.
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set() or bool(
            self._external_cancelled and self._external_cancelled()
        )

    def _check_cancelled(self) -> None:
        if self.is_cancelled():
            raise TTSCancelled("TTS 合成已取消")
        if self._closed:
            raise TTSError("TTS 客户端已关闭")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def synthesize(self, text: str, emotion: Emotion, *, seed: int | None = None) -> Path:
        """Synthesize speech; isolated auditions may supply a fixed seed.

        The app leaves seed unset, preserving the engine's random seed (-1).
        """
        self._check_cancelled()
        if seed is not None and (type(seed) is not int or not 0 <= seed <= 2**32 - 1):
            raise TTSError("测试 seed 必须是 0–4294967295 的整数")
        request_id = uuid.uuid4().hex[:8]
        # A settings change must not switch a request to another character or
        # port halfway through loading its references.
        config = self.settings.tts.model_copy(deep=True)
        if config.backend == "emotional_vits":
            return self._synthesize_emotional_vits(config, text, emotion, seed, request_id)
        endpoint = tts_base_url(config)
        profile_path = voice_profile_path(config)
        text_lang = prompt_lang = "ja"
        if profile_path and config.uses_autodl():
            raise TTSError("角色语音配置仅支持本地 GPT-SoVITS，不支持 AutoDL")
        logger.info(
            "TTS 合成准备 | ID=%s | 后端=%s | 地址=%s | "
            "文本字符=%s | 文本预览=%s | 情绪=%s",
            request_id,
            config.backend,
            endpoint,
            len(text),
            _text_preview(text),
            emotion,
        )
        if config.uses_autodl():
            manager = get_tts_service_manager()
            try:
                self._check_cancelled()
                if self._external_cancelled is None:
                    manager.ensure_running(config)
                else:
                    manager.ensure_running(config, cancelled=self.is_cancelled)
                self._check_cancelled()
                reference_audio_path, prompt_text = (
                    manager.autodl_reference(config, emotion)
                )
            except TTSServiceError as exc:
                raise TTSError(
                    f"AutoDL TTS 启动失败 [ID={request_id}]: {exc}"
                ) from exc
        else:
            try:
                state = locate_tts_assets(
                    configured_engine_root=config.engine_root,
                    configured_model_dir=config.model_dir,
                    configured_voice_profile=profile_path,
                )
                profile = state.voice_profile
                if profile_path:
                    if profile is None or profile.manifest_path != Path(profile_path).expanduser().resolve():
                        raise VoiceProfileError("定位的角色语音配置与当前角色不一致")
                elif profile is not None:
                    raise VoiceProfileError("默认丛雨语音不能使用其他角色的参考语音")
                if profile is not None:
                    reference = profile.reference(emotion)
                    reference_audio_path = str(reference.audio)
                    prompt_text = reference.transcript
                    text_lang = profile.text_lang
                    prompt_lang = profile.prompt_lang
                else:
                    if state.reference_root is None:
                        raise VoiceProfileError("丛雨 TTS 参考音频尚未下载完成")
                    reference_dir = state.reference_root / emotion
                    transcript_path = reference_dir / "asr.txt"
                    audio_files = sorted(
                        path for path in reference_dir.iterdir()
                        if path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".flac"}
                    ) if reference_dir.is_dir() else []
                    if not transcript_path.is_file() or not audio_files:
                        raise VoiceProfileError(f"缺少情绪参考语音: {emotion}")
                    reference_audio_path = str(audio_files[0].resolve())
                    prompt_text = transcript_path.read_text(encoding="utf-8").strip()
                    if not prompt_text:
                        raise VoiceProfileError(f"情绪参考文本为空: {emotion}")
            except (VoiceProfileError, OSError, UnicodeError) as exc:
                raise TTSError(f"角色语音配置无效 [ID={request_id}]: {exc}") from exc
            logger.info(
                "TTS 本地资源检查 | ID=%s | 配置引擎=%s | "
                "定位引擎=%s | 引擎就绪=%s | 配置模型=%s | "
                "GPT权重=%s | SoVITS权重=%s | 参考音频=%s | "
                "参考音频就绪=%s",
                request_id,
                config.engine_root,
                state.engine_root,
                state.engine_ready,
                config.model_dir,
                state.gpt_weight,
                state.sovits_weight,
                state.reference_root,
                state.reference_voices_ready,
            )
            if not state.model_ready:
                raise TTSError(
                    f"TTS 模型尚未下载完成 [ID={request_id}]"
                )
            try:
                self._check_cancelled()
                get_tts_service_manager().ensure_running(
                    config,
                    state=state,
                    cancelled=self.is_cancelled,
                )
                self._check_cancelled()
            except TTSServiceError as exc:
                raise TTSError(
                    f"TTS 服务启动失败 [ID={request_id}]: {exc}"
                ) from exc

            try:
                weights_key = (endpoint, state.gpt_weight, state.sovits_weight)
                if profile is None and self._weights_configured != weights_key:
                    configure_local_tts_weights(
                        endpoint,
                        state,
                        config.timeout_seconds,
                    )
                    self._weights_configured = weights_key
            except requests.RequestException as exc:
                raise TTSError(
                    f"TTS 模型加载失败 [ID={request_id}]: {exc}"
                ) from exc

        self._check_cancelled()
        params = {
            "text": text,
            "text_lang": text_lang,
            "ref_audio_path": reference_audio_path,
            "aux_ref_audio_paths": [],
            "prompt_text": prompt_text,
            "prompt_lang": prompt_lang,
            "top_k": 15,
            "top_p": 1,
            "temperature": 1,
            "text_split_method": "cut1",
            "batch_size": 1,
            "batch_threshold": 0.75,
            "split_bucket": True,
            "speed_factor": 1.0,
            "streaming_mode": False,
            "seed": -1 if seed is None else seed,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
            "sample_steps": 32,
            "super_sampling": False,
        }

        logger.info(
            "TTS 合成请求发出 | ID=%s | POST %s | 后端=%s | "
            "文本字符=%s | 文本预览=%s | 情绪=%s | 参考音频=%s | "
            "text_lang=%s | prompt_lang=%s | split=cut1 | "
            "sample_steps=32",
            request_id,
            endpoint,
            config.backend,
            len(text),
            _text_preview(text),
            emotion,
            reference_audio_path,
            text_lang,
            prompt_lang,
        )
        return self._request_audio(config, endpoint, params, request_id)

    def _synthesize_emotional_vits(self, config, text, emotion, seed, request_id) -> Path:
        # Import only the lightweight profile/manager here. Torch and Japanese
        # frontend globals stay in the helper's separate Python runtime.
        from aipet.core.emotional_vits_profile import (
            EmotionalVITSProfileError, load_emotional_vits_profile,
        )
        from aipet.core.emotional_vits_service import (
            EmotionalVITSServiceCancelled, EmotionalVITSServiceError,
            get_emotional_vits_service_manager,
        )

        try:
            profile = load_emotional_vits_profile(config.emotional_vits_profile)
            profile.require_emotion(emotion)  # Missing routes must not silently become calm.
            self._check_cancelled()
            endpoint = get_emotional_vits_service_manager().ensure_running(
                config, cancelled=self.is_cancelled,
            )
            self._check_cancelled()
        except EmotionalVITSServiceCancelled as exc:
            raise TTSCancelled(f"宁宁情绪语音已取消 [ID={request_id}]") from exc
        except (EmotionalVITSProfileError, EmotionalVITSServiceError) as exc:
            self._check_cancelled()
            raise TTSError(f"宁宁情绪语音准备失败 [ID={request_id}]: {exc}") from exc
        logger.info(
            "TTS 情绪模型请求 | ID=%s | 地址=%s | 情绪=%s | 字符=%s",
            request_id, endpoint.url, emotion, len(text),
        )
        return self._request_audio(
            config, endpoint.url,
            {"text": text, "emotion": emotion, "seed": seed}, request_id,
            headers={"Authorization": f"Bearer {endpoint.token}"},
        )

    def _request_audio(self, config, endpoint, params, request_id, *, headers=None) -> Path:
        self._check_cancelled()
        started_at = time.monotonic()
        request_options = {"json": params, "timeout": (10, config.timeout_seconds)}
        if headers is not None:
            request_options["headers"] = headers
            request_options["allow_redirects"] = False
        try:
            response = self.session.post(
                endpoint, **request_options,
            )
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - started_at) * 1_000
            logger.warning(
                "TTS 合成请求连接失败 | ID=%s | POST %s | %.0f ms | "
                "%s: %s",
                request_id,
                endpoint,
                elapsed_ms,
                type(exc).__name__,
                exc,
            )
            raise TTSError(
                f"TTS 请求失败 [ID={request_id}]: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        output_path = temporary_path = None
        committed = False
        try:
            self._check_cancelled()
            elapsed_ms = (time.monotonic() - started_at) * 1_000
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                detail = _response_text_summary(response)
                content_type = response.headers.get("Content-Type", "")
                logger.warning(
                    "TTS 合成请求被拒绝 | ID=%s | POST %s | HTTP %s | "
                    "%.0f ms | Content-Type=%s | 响应=%s | 服务日志=%s",
                    request_id,
                    endpoint,
                    response.status_code,
                    elapsed_ms,
                    content_type,
                    detail,
                    _service_log_hint(config.uses_autodl(), config.backend),
                )
                raise TTSError(
                    f"TTS 请求失败 [ID={request_id}]: "
                    f"HTTP {response.status_code}; "
                    f"服务端响应: {detail}"
                ) from exc

            content_type = response.headers.get("Content-Type", "")
            if not content_type.lower().startswith("audio/"):
                detail = _response_text_summary(response)
                logger.warning(
                    "TTS 合成响应格式异常 | ID=%s | POST %s | HTTP %s | "
                    "%.0f ms | Content-Type=%s | 响应=%s",
                    request_id,
                    endpoint,
                    response.status_code,
                    elapsed_ms,
                    content_type,
                    detail,
                )
                raise TTSError(
                    f"TTS 未返回音频 [ID={request_id}]: {detail}"
                )

            self._check_cancelled()
            content = response.content
            output_dir = get_cache_dir() / "voices"
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{uuid.uuid4().hex}.wav"
            temporary_path = output_path.with_suffix(".tmp")
            temporary_path.write_bytes(content)
            self._check_cancelled()
            temporary_path.replace(output_path)
            self._check_cancelled()
            logger.info(
                "TTS 合成完成 | ID=%s | POST %s | HTTP %s | %.0f ms | "
                "音频=%s 字节 | 文件=%s",
                request_id,
                endpoint,
                response.status_code,
                elapsed_ms,
                len(content),
                output_path,
            )
            committed = True
            return output_path
        except OSError as exc:
            raise TTSError(
                f"TTS 音频缓存写入失败 [ID={request_id}]: {exc}"
            ) from exc
        finally:
            try:
                response.close()
            finally:
                for path in (temporary_path, output_path if not committed else None):
                    if path is not None:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            logger.warning("TTS 临时音频清理失败：%s", path)


def _text_preview(text: str, limit: int = 80) -> str:
    normalized = " ".join(text.split())
    if len(normalized) > limit:
        normalized = normalized[:limit] + "…"
    return repr(normalized)


def _service_log_hint(uses_autodl: bool, backend: str = "local") -> str:
    if backend == "emotional_vits":
        from aipet.core.emotional_vits_service import get_emotional_vits_service_manager
        return str(get_emotional_vits_service_manager().startup_diagnostics().get("log_path")
                   or "<宁宁情绪语音子进程日志>")
    if uses_autodl:
        return "<AutoDL 远端 GPT-SoVITS 日志>"
    return str(get_cache_dir() / "logs" / "gpt-sovits-service.log")


def _response_text_summary(
    response: requests.Response,
    limit: int = 2_000,
) -> str:
    content_type = response.headers.get("Content-Type", "")
    if content_type.lower().startswith("audio/"):
        return f"<音频响应已省略，{len(response.content)} 字节>"
    try:
        text = " ".join(response.text.split())
    except Exception:
        return "<响应正文无法解码>"
    if not text:
        return "<空响应>"
    if len(text) > limit:
        return text[:limit] + f"…<已截断 {len(text) - limit} 字符>"
    return text

