"""Shared background workers."""

from __future__ import annotations

import threading
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from PyQt5.QtCore import QThread, pyqtSignal

from aipet.core.backends import (
    CharacterReply,
    BackendServiceError,
    ScreenAnalysis,
    create_backend,
    create_vision_backend,
)
from aipet.core.config import AppSettings
from aipet.core.runtime_logging import get_logger, log_event
from aipet.core.tts import TTSClient, TTSCancelled, TTSError


logger = get_logger("worker")


@dataclass(frozen=True)
class ConversationResult:
    reply: CharacterReply
    audio_paths: list[Path | None]
    user_text: str
    is_user_message: bool
    user_source: str = "typed"
    event_context: str | None = None
    episode_id: str = ""
    user_at: str = ""
    profile_id: str = ""
    user_evidence: tuple[str, ...] = ()


class ConversationWorker(QThread):
    """Run model and TTS requests without blocking the Qt event loop."""

    result_ready = pyqtSignal(object)
    stage_changed = pyqtSignal(str)
    error = pyqtSignal(str)
    warning = pyqtSignal(str)

    def __init__(
        self,
        settings: AppSettings,
        history: list[dict[str, str]],
        user_text: str,
        *,
        event_context: str | None = None,
        screen_memory: str | None = None,
        user_source: str = "typed",
        conversation_memory: str | None = None,
        character_context: str | None = None,
        episode_id: str | None = None,
        user_at: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.settings = settings.model_copy(deep=True)
        self.history = history
        self.user_text = user_text
        self.event_context = event_context
        self.screen_memory = screen_memory
        self.user_source = user_source
        self.conversation_memory = conversation_memory
        self.character_context = character_context
        evidence = [message["content"] for message in history
                    if message.get("role") == "user" and isinstance(message.get("content"), str)]
        if conversation_memory:
            try:
                records = json.loads(conversation_memory)
                for episode in records.get("episodes", []):
                    original = episode.get("user", {})
                    if original.get("role") == "user" and isinstance(original.get("content"), str):
                        evidence.append(original["content"])
            except (ValueError, TypeError, AttributeError):
                pass
        self.user_evidence = tuple(evidence)
        self.episode_id = (episode_id or str(uuid4())) if event_context is None else ""
        self.user_at = (
            user_at or datetime.now().astimezone().isoformat(timespec="seconds")
        ) if event_context is None else ""
        self._cancelled = threading.Event()
        self._active_tts: TTSClient | None = None

    def cancel(self) -> None:
        self._cancelled.set()
        self.requestInterruption()
        tts = self._active_tts
        if tts is not None:
            tts.cancel()

    def run(self) -> None:
        backend = tts = None
        audio_paths: list[Path | None] = []
        delivered = False
        try:
            if self._cancelled.is_set():
                return
            log_event(
                logger,
                "conversation.started",
                backend=self.settings.mode,
                source=(
                    "proactive_event"
                    if self.event_context is not None
                    else self.user_source
                ),
                history_messages=len(self.history),
                tts_enabled=self.settings.tts.enabled,
            )
            backend = create_backend(self.settings)
            arguments = (self.history, self.user_text, self.event_context,
                         self.screen_memory, self.user_source, self.conversation_memory)
            # Existing third-party/testing backends keep the original signature
            # until a Nene continuity context is actually available.
            reply = (backend.chat(*arguments, character_context=self.character_context)
                     if self.character_context else backend.chat(*arguments))
            if self._cancelled.is_set():
                log_event(logger, "conversation.cancelled")
                return

            audio_paths = [None] * len(reply.sentences)
            if self.settings.tts.enabled and getattr(reply, "should_speak", True):
                self.stage_changed.emit("voice")
                tts = TTSClient(self.settings, cancelled=self._cancelled.is_set)
                self._active_tts = tts
                for index, sentence in enumerate(reply.sentences):
                    if self._cancelled.is_set():
                        log_event(logger, "conversation.cancelled")
                        return
                    try:
                        audio_paths[index] = tts.synthesize(
                            sentence.ja,
                            sentence.emotion,
                        )
                    except TTSCancelled:
                        return
                    except TTSError as exc:
                        if self._cancelled.is_set():
                            return
                        logger.warning("TTS 合成失败：%s", exc)
                        self.warning.emit(str(exc))

            if self._cancelled.is_set():
                log_event(logger, "conversation.cancelled")
                return

            self.result_ready.emit(
                ConversationResult(
                    reply=reply,
                    audio_paths=audio_paths,
                    user_text=self.user_text,
                    is_user_message=self.event_context is None,
                    user_source=self.user_source,
                    event_context=self.event_context,
                    episode_id=self.episode_id,
                    user_at=self.user_at,
                    profile_id=self.settings.character.profile_id,
                    user_evidence=self.user_evidence,
                )
            )
            delivered = True
            log_event(
                logger,
                "conversation.completed",
                sentence_count=len(reply.sentences),
                outfit=reply.outfit or self.settings.character.outfit,
                emotions=[
                    sentence.emotion for sentence in reply.sentences
                ],
                audio_count=sum(path is not None for path in audio_paths),
            )
        except Exception as exc:
            if not self._cancelled.is_set():
                if isinstance(exc, BackendServiceError):
                    logger.warning("对话服务未提供回复 | category=%s", exc.category)
                    self.error.emit(exc.user_message)
                else:
                    logger.exception("对话任务失败")
                    self.error.emit("The reply could not be generated. Please try again."
                                    if self.settings.ui_language == "en"
                                    else "本次回复未能生成，请稍后重试或检查模型设置。")
        finally:
            self._active_tts = None
            for resource in (tts, getattr(backend, "session", None)):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        logger.warning("对话连接关闭失败", exc_info=True)
            if not delivered:
                self._remove_audio(audio_paths)

    @staticmethod
    def _remove_audio(audio_paths: list[Path | None]) -> None:
        for path in audio_paths:
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


class VisionWorker(QThread):
    """Analyze a screenshot using the configured vision model."""

    analysis_ready = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(
        self,
        settings: AppSettings,
        image_path: Path,
        previous_analysis: ScreenAnalysis | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.image_path = image_path
        self.previous_analysis = previous_analysis
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        self.requestInterruption()

    def run(self) -> None:
        try:
            log_event(
                logger,
                "vision.started",
                backend=self.settings.vision.provider,
                has_previous_analysis=self.previous_analysis is not None,
            )
            analysis = create_vision_backend(self.settings).describe_image(
                self.image_path,
                self.previous_analysis,
            )
            if not self._cancelled.is_set():
                self.analysis_ready.emit(analysis)
                log_event(
                    logger,
                    "vision.completed",
                    **analysis.model_dump(mode="json"),
                )
            else:
                log_event(logger, "vision.cancelled")
        except Exception as exc:
            if not self._cancelled.is_set():
                logger.exception("屏幕视觉分析失败")
                self.error.emit(str(exc))
        finally:
            try:
                self.image_path.unlink(missing_ok=True)
            except OSError:
                pass

