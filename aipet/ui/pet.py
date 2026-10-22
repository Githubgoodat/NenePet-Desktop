"""Shared Qt desktop-pet widget."""

from __future__ import annotations

import copy
import json
import os
import textwrap
import time
import uuid
import wave
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import cv2
from PyQt5.QtCore import QEvent, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QPainter,
    QPixmap,
    QScreen,
)
from PyQt5.QtMultimedia import QSound
from PyQt5.QtWidgets import QLabel

from aipet.core.workers import (
    ConversationResult,
    ConversationWorker,
    VisionWorker,
)
from aipet.core.backends import ScreenAnalysis
from aipet.core.config import AppSettings, PROJECT_ROOT, get_cache_dir, get_user_data_dir
from aipet.core.generate import generate_fgimage
from aipet.core.portraits import default_layers, layers_for
from aipet.core.runtime_logging import get_logger
from aipet.core.storage import HistoryStore, ScreenMemoryEntry, ScreenMemoryStore
from aipet.core.time_utils import build_time_context
from aipet.ui.audio_playback import AudioPlayback
from aipet.platforms import PlatformRuntime, get_platform_runtime


SCREEN_PIXEL_CHANGE_THRESHOLD = 0.008
PROACTIVE_COOLDOWN_SECONDS = 180
TOPMOST_WATCHDOG_INTERVAL_MS = 2_000
logger = get_logger("window")


def wrap_text(text: str, width: int = 10) -> str:
    return "\n".join(
        textwrap.wrap(
            text,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
        )
    )


def get_idle_seconds(runtime: PlatformRuntime | None = None) -> float:
    """Compatibility hook delegating idle detection to the active platform."""

    return (runtime or get_platform_runtime()).input.idle_seconds()


def native_topmost_available(
    runtime: PlatformRuntime | None = None,
) -> bool:
    """Compatibility hook delegating capability checks to the platform."""

    return (runtime or get_platform_runtime()).windowing.topmost_available()


def ensure_window_topmost(
    window_id: int,
    runtime: PlatformRuntime | None = None,
) -> bool:
    """Compatibility hook delegating native window work to the platform."""

    return (runtime or get_platform_runtime()).windowing.ensure_topmost(
        window_id
    )


class Murasame(QLabel):
    pause_changed = pyqtSignal(bool)
    test_mode_changed = pyqtSignal(bool)
    notification = pyqtSignal(str, str)
    interaction_started = pyqtSignal()

    def __init__(
        self,
        settings: AppSettings,
        platform_runtime: PlatformRuntime | None = None,
    ):
        super().__init__()
        self._platform_runtime = platform_runtime or get_platform_runtime()
        self.settings = settings.model_copy(deep=True)
        self._closing = False
        self._viewerex = None
        self._avatar_last_emotion = "平静"
        self._viewer_layout = None
        self._viewer_position_restored = False
        self._viewer_had_saved_position = (
            settings.display.window_x is not None
            and settings.display.window_y is not None
        )
        self.pet_name = self._character_name()
        self._update_window_identity()

        self.full_text = ""
        self.display_text = ""
        self.typing_prefix = ""
        self.typing_index = 0
        self.typing_timer = QTimer(self)
        self.typing_timer.setInterval(40)
        self.typing_timer.timeout.connect(self._typing_step)
        self.thinking_timer = QTimer(self)
        self.thinking_timer.setInterval(350)
        self.thinking_timer.timeout.connect(self._thinking_step)
        self._thinking_dot_count = 0
        self._thinking_label = ""
        self.status_clear_timer = QTimer(self)
        self.status_clear_timer.setSingleShot(True)
        self.status_clear_timer.timeout.connect(self._clear_dialogue)

        self.input_mode = False
        self.input_buffer = ""
        self.preedit_text = ""
        self.touch_head = False
        self.head_press_x: int | None = None
        self.drag_offset = None
        self._viewer_drag_origin = None

        self._base_font_size = 40
        self._base_text_x_offset = 140
        self._base_text_y_offset = -100
        self._base_border_size = 2
        self._current_scale = 1.0
        self._source_portrait_pixmap: QPixmap | None = None
        self._active_screen_key: tuple[object, ...] | None = None
        self._font_family = self._load_font()
        self._update_text_scaling()
        self._screen_resize_timer = QTimer(self)
        self._screen_resize_timer.setSingleShot(True)
        self._screen_resize_timer.setInterval(120)
        self._screen_resize_timer.timeout.connect(
            self._adapt_to_current_screen
        )
        self._topmost_error_logged = False
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(TOPMOST_WATCHDOG_INTERVAL_MS)
        self._topmost_timer.timeout.connect(self._ensure_topmost)

        self.history_store = HistoryStore(
            limit=self.settings.history_limit,
            separate_proactive=self.settings.character.profile_id == "nene",
        )
        self.history = self.history_store.load()
        logger.info(
            "对话历史已加载 | dialogue_messages=%d | separate_proactive=%s | pending_proactive=%s | conversation_episodes=%d",
            len(self.history), self.history_store.separate_proactive,
            len(self.history_store.context_messages(self.history)) > len(self.history),
            self.history_store.conversation_count,
        )
        self.screen_memory_store = ScreenMemoryStore(limit=200 if self.settings.character.profile_id == "nene" else 12)
        self._test_mode = False
        self._formal_session = None
        self._proactive_awaiting_user = False
        self._pause_until_user = False
        self._last_proactive_reason_key = ""
        self._idle_resume_at = time.monotonic()
        from aipet.core.nene_continuity import NeneContinuityStore
        self.continuity_store = NeneContinuityStore(get_user_data_dir() / "nene_continuity.json")
        self.response_target_timer = QTimer(self)
        self.response_target_timer.setSingleShot(True)
        self.response_target_timer.timeout.connect(self._on_response_target_reached)
        self._response_waiting_generation = None
        self._response_target_elapsed = False
        self._generation = 0
        self._voice_input_epoch = 0
        self._voice_input_pending = False
        self._workers: dict[int, ConversationWorker] = {}
        self._vision_worker: VisionWorker | None = None
        self._last_screen_thumbnail = None
        self._last_screen_analysis: ScreenAnalysis | None = None
        self._screen_baseline_ready = False
        self._last_spoken_screen_event = ""
        self._proactive_cooldown_until = 0.0
        self._playback_result: ConversationResult | None = None
        self._delivery_result: ConversationResult | None = None
        self._delivery_count = 0
        self._playback_index = 0
        self._sound: QSound | None = None
        self._audio_clock_active = False
        self._audio_clock_generation = 0
        self._audio_clock_player = None
        self.playback_timer = QTimer(self)
        self.playback_timer.setSingleShot(True)
        self.playback_timer.timeout.connect(self._play_next_sentence)

        self._dnd_enabled = self.settings.idle.do_not_disturb
        self.idle_thinking_triggered = False
        self.idle_away_triggered = False
        self.away_trigger_time: float | None = None
        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(1000)
        self.idle_timer.timeout.connect(self.check_idle_state)
        self.idle_timer.start()

        self.screenshot_timer = QTimer(self)
        self.screenshot_timer.timeout.connect(self._capture_screen)

        self._platform_runtime.windowing.configure_widget(self)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.setMouseTracking(True)

        if os.getenv("AIPET_RENDERER", "").lower() == "viewerex":
            from aipet.ui.live2d_renderer import ViewerEXConfig, ViewerEXRenderer

            self._viewerex = ViewerEXRenderer(ViewerEXConfig.from_environment(), self)
            self._viewerex.warning.connect(
                lambda message: self.notification.emit("Live2DViewerEX", message)
            )
            self._viewerex.model_tapped.connect(self.begin_text_input)
            Murasame._sync_avatar_mood(self)
            QTimer.singleShot(0, self._viewerex.start)

        self.update_portrait(
            default_layers(
                self.settings.character.portrait,
                self.settings.character.outfit,
            ),
            self.settings.character.portrait,
            self.settings.character.outfit,
        )
        self._apply_automatic_behavior_settings()

    def _character_name(self) -> str:
        character = self.settings.character
        configured = (
            getattr(character, "name_en", "")
            if self.settings.ui_language == "en"
            else getattr(character, "name", "")
        )
        configured = configured or getattr(character, "name", "")
        if configured:
            return configured
        override = os.getenv("AIPET_CHARACTER_NAME", "").strip()
        if override:
            return override
        if os.getenv("AIPET_RENDERER", "").lower() == "viewerex":
            return "Nene" if self.settings.ui_language == "en" else "宁宁"
        return "Murasame" if self.settings.ui_language == "en" else "丛雨"

    def _avatar_performance_available(self) -> bool:
        viewer = getattr(self, "_viewerex", None)
        return bool(viewer is not None
            and getattr(getattr(getattr(self, "settings", None), "character", None), "profile_id", "") == "nene"
            and bool(getattr(viewer, "performance_enabled", False))
            and callable(getattr(viewer, "play_speech", None)))

    def _sync_avatar_mood(self) -> None:
        """Visual interpretation only: never creates a relationship or memory."""
        if not Murasame._avatar_performance_available(self):
            return
        emotion = getattr(self, "_avatar_last_emotion", "平静")
        strength = 0.55
        mood = getattr(getattr(self, "continuity_store", None), "mood", "").casefold()
        # Free-text continuity may be multilingual. This chooses a visual
        # approximation, not an affect score or a relationship decision.
        families = (
            (("失望", "难过", "委屈", "disappoint", "hurt", "sad", "悲し", "がっかり"), "生气", 0.30),
            (("生气", "愤怒", "不满", "angry", "anger", "不機嫌", "怒"), "生气", 0.72),
            (("担心", "着急", "焦虑", "worri", "anxious", "不安", "心配"), "着急", 0.50),
            (("害羞", "羞涩", "shy", "embarrass", "照れ", "恥ずかし"), "害羞", 0.50),
            (("开心", "高兴", "期待", "happy", "excited", "嬉し", "楽し"), "高兴", 0.55),
            (("平静", "释然", "安心", "calm", "reliev", "落ち着"), "平静", 0.55),
        )
        for words, candidate, amount in families:
            if any(word in mood for word in words):
                emotion, strength = candidate, amount
                break
        self._viewerex.set_base_emotion(emotion, strength=strength)
        self._viewerex.set_paused(getattr(self, "_dnd_enabled", False))

    def _avatar_activity(self, phase: str) -> None:
        if not Murasame._avatar_performance_available(self):
            return
        if getattr(self, "_dnd_enabled", False) or getattr(self, "_closing", False):
            return
        # Targets are the local dialogue area and a short thinking glance;
        # no camera or assumption about where the user's eyes actually are.
        attention = (0.0, 0.22) if phase == "listening" else ((-0.16, 0.10) if phase == "thinking" else (0.0, 0.0))
        self._viewerex.set_activity(phase, attention=attention)

    def _restore_avatar_face(self) -> None:
        if getattr(self, "_viewerex", None) is None:
            return
        if Murasame._avatar_performance_available(self):
            self._viewerex.finish_speech()
            Murasame._sync_avatar_mood(self)
            Murasame._avatar_activity(self, "listening" if getattr(self, "input_mode", False) else "idle")
        else:
            self._viewerex.set_emotion("平静")

    def _update_window_identity(self) -> None:
        title = self._localized(
            f"{self.pet_name} · Desktop Pet",
            f"{self.pet_name} · 桌宠",
        )
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        self.setAccessibleDescription(self._localized(
            "Click the character's body to type; use the middle mouse button on the character or visible subtitles to drag them together.",
            "点击人物身体输入；在人物或可见字幕上按住鼠标中键，一起拖动。",
        ))

    def place_live2d_overlay(self) -> None:
        """Call after the application's normal window-position restoration.

        Only the transparent Qt window moves here. ViewerEX's model position
        remains untouched, including when a saved overlay position is restored.
        """
        if self._viewerex is None:
            return
        self._prepare_live2d_overlay()
        managed_initial_placement = (
            not self._viewer_position_restored
            and self._viewerex.config.managed_position
            and self._viewerex.config.position is not None
        )
        if managed_initial_placement or (
            not self._viewer_position_restored
            and not self._viewer_had_saved_position
        ) or (
            self.settings.display.window_x is None
            or self.settings.display.window_y is None
        ):
            self.move(self._viewer_layout.geometry.topLeft())
        self._viewer_position_restored = True
        # A saved overlay position may belong to a previous DPI/resolution.
        # Refit the transparent window without repositioning the Viewer model.
        screen = self._configured_screen()
        if screen is not None and not screen.geometry().contains(self.geometry()):
            self.move(self._viewer_layout.geometry.topLeft())
        self.remember_window_position()
        self.update()
        body_global = QRect(self._viewer_layout.body_rect)
        body_global.moveTopLeft(self.mapToGlobal(body_global.topLeft()))
        logger.info(
            "Live2D 点击层定位 | geometry=%s | body_global=%s | visible=%s | enabled=%s",
            self.geometry().getRect(), body_global.getRect(), self.isVisible(), self.isEnabled(),
        )

    def _prepare_live2d_overlay(self) -> None:
        screen = self._configured_screen()
        screen_geometry = screen.geometry() if screen is not None else QRect(0, 0, 2560, 1440)
        self._viewer_layout = self._viewerex.config.layout(screen_geometry)
        layout = self._viewer_layout
        self.resize(layout.geometry.size())
        self._current_scale = self._viewerex.config.font_size / self._base_font_size
        self._update_text_scaling()

    def _dialogue_rect(self) -> QRect:
        if self._viewer_layout is not None:
            return self._viewer_layout.dialogue_rect
        rect = self.rect()
        return rect.adjusted(
            self.text_x_offset,
            self.text_y_offset,
            -self.text_x_offset,
            -rect.height() // 2 + self.text_y_offset,
        )

    def _load_font(self) -> str:
        font_path = PROJECT_ROOT / "思源黑体Bold.otf"
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
        return families[0] if families else QFont().family()

    def _english_ui(self) -> bool:
        return self.settings.ui_language == "en"

    def _localized(self, english: str, chinese: str) -> str:
        return english if self._english_ui() else chinese

    def _wrap_dialogue(self, text: str) -> str:
        return wrap_text(text, width=28 if self._english_ui() else 10)

    def _speaker_prefix(self, speaker_name: str | None = None) -> str:
        name = speaker_name or self.pet_name
        return f"[{name}]\n" if self._english_ui() else f"【{name}】\n"

    def _input_display(self, content: str) -> str:
        prefix = self._speaker_prefix(self.settings.character.user_name)
        if self._english_ui():
            return f'{prefix}  "{content}"'
        return f"{prefix}  「{content}」"

    def apply_settings(self, settings: AppSettings) -> None:
        if self._closing:
            return
        # Finish any displayed prefix under its original language and stores.
        # Changing settings first would strand a partially typed old subtitle.
        self._cancel_active_jobs(include_vision=True)
        self._clear_dialogue()
        self.input_mode = False
        self.input_buffer = self.preedit_text = ""
        if getattr(self, "_test_mode", False):
            self.set_test_mode(False)
        old_portrait = self.settings.character.portrait
        old_outfit = self.settings.character.outfit
        was_dnd_enabled = self._dnd_enabled
        current_layers = getattr(
            self,
            "_current_layers",
            default_layers(old_portrait),
        )
        current_portrait = getattr(self, "_current_portrait", old_portrait)
        current_outfit = getattr(self, "_current_outfit", old_outfit)
        self.settings = settings.model_copy(deep=True)
        self.pet_name = self._character_name()
        self._update_window_identity()
        self._dnd_enabled = self.settings.idle.do_not_disturb
        if self._dnd_enabled:
            self._cancel_active_jobs(include_vision=True)
            self._clear_dialogue()
            self.input_mode = False
            self.input_buffer = self.preedit_text = ""
        elif was_dnd_enabled:
            self._reset_screen_observation()
            self._idle_resume_at = time.monotonic()
            self._proactive_awaiting_user = True
            self._pause_until_user = True
        self.history_store.limit = self.settings.history_limit
        self.history_store.set_separate_proactive(
            self.settings.character.profile_id == "nene"
        )
        self.history = self.history[-self.settings.history_limit :]
        self.history_store.save(self.history)
        self._reset_screen_observation()

        if (
            old_portrait != self.settings.character.portrait
            or old_outfit != self.settings.character.outfit
        ):
            portrait = self.settings.character.portrait
            outfit = self.settings.character.outfit
            self.update_portrait(
                default_layers(portrait, outfit),
                portrait,
                outfit,
            )
        else:
            self.update_portrait(
                current_layers,
                current_portrait,
                current_outfit,
            )
        self._reset_idle_state()
        self._apply_automatic_behavior_settings()
        Murasame._sync_avatar_mood(self)
        Murasame._avatar_activity(self, "idle")
        if was_dnd_enabled != self._dnd_enabled:
            self.pause_changed.emit(self._dnd_enabled)

    def _apply_automatic_behavior_settings(self) -> None:
        if self._closing:
            return
        self.screenshot_timer.stop()
        if (
            self.settings.vision.enabled
            and self.settings.supports_vision()
            and not self._dnd_enabled
        ):
            self.screenshot_timer.start(
                self.settings.vision.interval_seconds * 1000
            )
        if self._dnd_enabled:
            self.idle_timer.stop()
        elif not self.idle_timer.isActive():
            self.idle_timer.start()

    def set_screenshot_enabled(self, enabled: bool) -> None:
        if enabled and not self.settings.supports_vision():
            self.settings.vision.enabled = False
            self.notification.emit(
                "Screen vision unavailable",
                "DeepSeek mode is chat-only. Choose Alibaba Cloud or Ollama.",
            )
            return
        if bool(enabled) != self.settings.vision.enabled:
            self._reset_screen_observation()
        self.settings.vision.enabled = bool(enabled)
        self._apply_automatic_behavior_settings()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if native_topmost_available(self._platform_runtime):
            self._topmost_timer.start()
            QTimer.singleShot(0, self._ensure_topmost)

    def hideEvent(self, event) -> None:
        self._topmost_timer.stop()
        super().hideEvent(event)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in {
            QEvent.ActivationChange,
            QEvent.WindowStateChange,
        }:
            QTimer.singleShot(0, self._ensure_topmost)

    def _ensure_topmost(self) -> None:
        if (
            not native_topmost_available(self._platform_runtime)
            or not self.isVisible()
        ):
            return
        try:
            ensure_window_topmost(
                int(self.winId()),
                self._platform_runtime,
            )
        except OSError as exc:
            if not self._topmost_error_logged:
                logger.warning("无法重新确认桌宠窗口置顶状态：%s", exc)
                self._topmost_error_logged = True
        else:
            self._topmost_error_logged = False

    def is_screenshot_enabled(self) -> bool:
        return self.settings.vision.enabled

    def set_dnd_enabled(self, enabled: bool) -> None:
        was_enabled = self._dnd_enabled
        self._dnd_enabled = bool(enabled)
        self.settings.idle.do_not_disturb = self._dnd_enabled
        if self._dnd_enabled:
            self._cancel_active_jobs(include_vision=True)
            self._reset_screen_observation()
            self.input_mode = False
            self.input_buffer = self.preedit_text = ""
            self._clear_dialogue()
            self._reset_idle_state()
        elif was_enabled:
            self._reset_screen_observation()
            self._reset_idle_state()
            self._idle_resume_at = time.monotonic()
            self._proactive_awaiting_user = True
            self._pause_until_user = True
        self._apply_automatic_behavior_settings()
        Murasame._sync_avatar_mood(self)
        if was_enabled != self._dnd_enabled:
            signal = getattr(self, "pause_changed", None)
            if signal is not None:
                signal.emit(self._dnd_enabled)

    def is_dnd_enabled(self) -> bool:
        return self._dnd_enabled

    def is_test_mode(self) -> bool:
        return getattr(self, "_test_mode", False)

    def set_test_mode(self, enabled: bool) -> None:
        """Fork runtime state; never merge test experiences back into reality."""
        enabled = bool(enabled)
        if self._closing or enabled == self.is_test_mode():
            return
        if self.settings.character.profile_id != "nene":
            return
        self._cancel_active_jobs(include_vision=True)
        self._reset_screen_observation()
        self.input_mode = False
        self.input_buffer = self.preedit_text = ""
        self._clear_dialogue()
        if enabled:
            destination = get_user_data_dir() / "test-sessions" / uuid.uuid4().hex
            test_history = copy.deepcopy(self.history_store)
            test_history.path = destination / "history.json"
            test_messages = copy.deepcopy(self.history)
            test_screens = copy.deepcopy(self.screen_memory_store)
            test_screens.path = destination / "screen_memory.json"
            try:
                test_continuity = self.continuity_store.clone_for_test(destination / "nene_continuity.json")
                test_history.save(test_messages)
                test_screens.save()
            except (OSError, ValueError):
                self.test_mode_changed.emit(False)
                self.show_text(self._localized(
                    "The test session could not be created; real memories are unchanged.",
                    "无法建立测试副本，正式相处记录保持原状。",
                ), typing=False, speaker_name=self._localized("System", "系统"))
                return
            self._formal_session = (
                self.history_store, self.history, self.screen_memory_store,
                self.continuity_store, self.settings.character.outfit,
                getattr(self, "_avatar_last_emotion", "平静"),
            )
            self.history_store, self.history = test_history, test_messages
            self.screen_memory_store, self.continuity_store = test_screens, test_continuity
        else:
            (self.history_store, self.history, self.screen_memory_store,
             self.continuity_store, outfit, avatar_emotion) = self._formal_session
            self.settings.character.outfit = outfit
            self._avatar_last_emotion = avatar_emotion
            self._formal_session = None
        self._test_mode = enabled
        Murasame._sync_avatar_mood(self)
        Murasame._avatar_activity(self, "idle")
        self._proactive_awaiting_user = True
        self._pause_until_user = True
        self._idle_resume_at = time.monotonic()
        self._reset_idle_state()
        if not enabled:
            self.update_portrait(default_layers(self.settings.character.portrait, self.settings.character.outfit),
                                 self.settings.character.portrait, self.settings.character.outfit)
        title = self.windowTitle().removesuffix(" [测试]").removesuffix(" [TEST]")
        self.setWindowTitle(title + (self._localized(" [TEST]", " [测试]") if enabled else ""))
        self.test_mode_changed.emit(enabled)
        if not self._dnd_enabled:
            self.show_text(self._localized(
                "Test mode: changes stay in this test session." if enabled else "Real conversation restored.",
                "已进入测试模式，本次相处变化仅留在测试记录。" if enabled else "已恢复正式相处记录。",
            ), typing=False, speaker_name=self._localized("System", "系统"))

    def _reset_idle_state(self) -> None:
        self.idle_thinking_triggered = False
        self.idle_away_triggered = False
        self.away_trigger_time = None

    def _reset_screen_observation(self) -> None:
        self._screen_observation_generation = getattr(self, "_screen_observation_generation", 0) + 1
        self._last_screen_thumbnail = None
        self._last_screen_analysis = None
        self._screen_baseline_ready = False
        self._last_spoken_screen_event = ""

    def _proactive_event_blocked(self) -> bool:
        return (
            self._closing
            or self._dnd_enabled
            or self.input_mode
            or getattr(self, "_utility_busy", False)
            or getattr(self, "_voice_input_pending", False)
            or bool(self._workers)
            or self._playback_result is not None
        )

    def _try_start_proactive_event(self, event_context: str, *, event_key: str | None = None) -> bool:
        if self._proactive_event_blocked():
            return False
        reason_key = event_key or event_context
        if self.settings.character.profile_id == "nene":
            if getattr(self, "_pause_until_user", False):
                return False
            if (getattr(self, "_proactive_awaiting_user", False)
                    and reason_key == getattr(self, "_last_proactive_reason_key", reason_key)):
                return False

        now = time.monotonic()
        if now < self._proactive_cooldown_until:
            return False

        interaction = getattr(self.settings, "interaction", None)
        cooldown = getattr(interaction, "proactive_cooldown_seconds", PROACTIVE_COOLDOWN_SECONDS)
        self._proactive_cooldown_until = now + cooldown
        self._last_proactive_reason_key = reason_key
        self.start_thread(event_context, role="system")
        return True

    def check_idle_state(self) -> None:
        if self._closing or self._dnd_enabled:
            return
        if self.settings.character.profile_id == "nene" and hasattr(self, "continuity_store"):
            if getattr(self, "_proactive_awaiting_user", False):
                return
            # Idle activity is a time to consider a real pending topic, never
            # an invented claim that the user is lonely, tired or has returned.
            idle = get_idle_seconds(self._platform_runtime)
            elapsed = time.monotonic() - getattr(self, "_idle_resume_at", 0)
            if min(idle, elapsed) < self.settings.idle.thinking_minutes * 60:
                return
            reason = self.continuity_store.proactive_reason()
            if reason:
                self._try_start_proactive_event(json.dumps({
                    "kind": "pending_expectation", "pending_reason": reason,
                    "observation": "检测到输入闲置；不代表约定时间已经到来，也不代表用户需要提醒。仅在确有合适内容时开口，否则保持安静。",
                    "current_time": build_time_context(self.settings.ui_language),
                }, ensure_ascii=False), event_key="expectation:" + reason)
            return

        thinking_seconds = self.settings.idle.thinking_minutes * 60
        away_seconds = self.settings.idle.away_minutes * 60
        idle_seconds = get_idle_seconds(self._platform_runtime)

        if (
            idle_seconds <= thinking_seconds
            and self.idle_away_triggered
            and self.away_trigger_time is not None
        ):
            if time.time() - self.away_trigger_time >= 30:
                return_event = (
                    self._localized(
                        "Mouse or keyboard input has resumed after an idle "
                        "period. This does not establish that the user left "
                        "or returned, or that they said anything. Respond "
                        "naturally from the actual conversation and your "
                        "current feelings; do not invent a prior topic.",
                        "检测到一段时间没有鼠标或键盘输入后，输入活动恢复。"
                        "这不代表对方离开后返回，也不代表对方说了话。"
                        "依据实际对话和当前心情自然接话，不预设需要继续的话题。",
                    )
                    if self.settings.character.profile_id == "nene"
                    else
                    self._localized(
                        "The user has just returned to the computer. Briefly "
                        "welcome Master back and naturally ask whether they "
                        "would like to continue what they were doing.",
                        "用户刚刚回到电脑前。简短欢迎主人回来，"
                        "并自然地问是否要继续刚才的事情。",
                    )
                )
                self._try_start_proactive_event(return_event)
            self._reset_idle_state()
            return

        if idle_seconds <= thinking_seconds:
            self.idle_thinking_triggered = False
            self.idle_away_triggered = False
            return

        if idle_seconds >= away_seconds and not self.idle_away_triggered:
            self.idle_away_triggered = True
            self.away_trigger_time = time.time()
            away_event = (
                self._localized(
                    "No mouse or keyboard input has been detected for a "
                    "longer period. Input inactivity alone does not establish "
                    "what the user is doing, how they feel, or whether they "
                    "are present. Respond naturally from the actual "
                    "conversation and your current feelings without inventing "
                    "a prior topic or user utterance.",
                    "检测到较长时间没有鼠标或键盘输入。仅凭输入静止，"
                    "无法得知对方在做什么、身体状态或是否在电脑前。"
                    "依据实际对话和当前心情自然接话，不虚构已有话题或对方发言。",
                )
                if self.settings.character.profile_id == "nene"
                else
                self._localized(
                    "The user has been away from the computer for a while. "
                    "Gently ask whether Master is still there and remind them "
                    "to take an appropriate break.",
                    "用户已经离开电脑一段时间。轻声问主人是否还在，"
                    "并提醒适当休息。",
                )
            )
            self._try_start_proactive_event(away_event)
            return

        if not self.idle_thinking_triggered:
            self.idle_thinking_triggered = True
            self._try_start_proactive_event(
                (
                    self._localized(
                        "No mouse or keyboard input has been detected for "
                        "a while. Input inactivity alone does not establish "
                        "what the user is doing, how they feel, or whether "
                        "they are present. Respond naturally from the actual "
                        "conversation and your current feelings without "
                        "inventing a prior topic or user utterance.",
                        "检测到一段时间没有鼠标或键盘输入。仅凭输入静止，"
                        "无法得知对方在做什么、身体状态或是否在电脑前。"
                        "依据实际对话和当前心情自然接话，不虚构已有话题或对方发言。",
                    )
                    if self.settings.character.profile_id == "nene"
                    else
                    self._localized(
                        "The user has not provided input for a while and may be "
                        "thinking, daydreaming, or resting. Check in gently "
                        "without being intrusive.",
                        "用户一段时间没有输入，可能正在思考、发呆或休息。"
                        "温柔地关心一下，避免过分打扰。",
                    )
                ),
            )

    def _capture_screen(self) -> None:
        if (
            self._closing
            or self._dnd_enabled
            or not self.settings.vision.enabled
            or (
                self._vision_worker is not None
                and self._vision_worker.isRunning()
            )
        ):
            return

        screens = QGuiApplication.screens()
        screen_index = self.settings.display.screen_index
        screen = (
            screens[screen_index]
            if 0 <= screen_index < len(screens)
            else QGuiApplication.primaryScreen()
        )
        if screen is None:
            self.notification.emit("屏幕分析", "没有找到可用显示器")
            return

        screenshot_dir = get_cache_dir() / "screens"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        image_path = screenshot_dir / f"{uuid.uuid4().hex}.png"
        pixmap = screen.grabWindow(0)
        if pixmap.isNull():
            self.notification.emit("屏幕分析", "屏幕截图失败")
            return
        if not pixmap.save(str(image_path), "PNG"):
            self.notification.emit("屏幕分析", "屏幕截图失败")
            return

        thumbnail = self._load_screen_thumbnail(image_path)
        if thumbnail is not None:
            previous_thumbnail = self._last_screen_thumbnail
            self._last_screen_thumbnail = thumbnail
            if (
                previous_thumbnail is not None
                and not self._pixels_changed(
                    previous_thumbnail,
                    thumbnail,
                )
            ):
                image_path.unlink(missing_ok=True)
                return

        worker = VisionWorker(
            self.settings.model_copy(deep=True),
            image_path,
            previous_analysis=self._last_screen_analysis,
            parent=self,
        )
        self._vision_worker = worker
        observation_generation = getattr(self, "_screen_observation_generation", 0)
        worker.analysis_ready.connect(
            lambda analysis, current=observation_generation: self._on_screen_analysis(
                analysis, observation_generation=current,
            )
        )
        worker.error.connect(
            lambda message, current=observation_generation: self.notification.emit("屏幕分析失败", message)
            if not self._closing and not self._dnd_enabled
            and current == getattr(self, "_screen_observation_generation", 0) else None
        )
        worker.finished.connect(lambda: self._finish_vision_worker(worker))
        worker.start()

    def _finish_vision_worker(self, worker: VisionWorker) -> None:
        if self._vision_worker is worker:
            self._vision_worker = None
        worker.deleteLater()

    @staticmethod
    def _load_screen_thumbnail(image_path: Path):
        image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return None
        return cv2.resize(image, (96, 54), interpolation=cv2.INTER_AREA)

    @staticmethod
    def _pixels_changed(
        previous,
        current,
        threshold: float = SCREEN_PIXEL_CHANGE_THRESHOLD,
    ) -> bool:
        if previous.shape != current.shape:
            return True
        difference = cv2.absdiff(previous, current)
        return float(difference.mean()) / 255.0 >= threshold

    @staticmethod
    def _screen_event_key(analysis: ScreenAnalysis) -> str:
        parts = (
            analysis.software,
            analysis.activity,
            analysis.topic,
            analysis.change_summary,
        )
        return "|".join(" ".join(part.casefold().split()) for part in parts)

    def _on_screen_analysis(
        self, analysis: ScreenAnalysis, *, observation_generation: int | None = None,
    ) -> None:
        if self._closing or self._dnd_enabled:
            return
        if (
            observation_generation is not None
            and observation_generation != getattr(self, "_screen_observation_generation", 0)
        ):
            return

        self._last_screen_analysis = analysis
        if not self._screen_baseline_ready:
            self._screen_baseline_ready = True
            return
        if (
            not analysis.significant_change
            or not analysis.change_summary.strip()
        ):
            return

        event_key = self._screen_event_key(analysis)
        if not event_key:
            return
        self.screen_memory_store.remember(
            ScreenMemoryEntry.now(
                software=analysis.software,
                activity=analysis.activity,
                topic=analysis.topic,
                change_summary=analysis.change_summary,
            )
        )
        if event_key == self._last_spoken_screen_event:
            return

        if self._english_ui():
            details = ["The screen has changed significantly."]
            if analysis.software:
                details.append(f"Application: {analysis.software}")
            if analysis.activity:
                details.append(f"Current activity: {analysis.activity}")
            if analysis.topic:
                details.append(f"Topic: {analysis.topic}")
            if analysis.change_summary:
                details.append(f"Change summary: {analysis.change_summary}")
            details.append(
                f"Current time: {build_time_context('en')}"
            )
        else:
            details = ["屏幕发生了明显变化。"]
            if analysis.software:
                details.append(f"软件：{analysis.software}")
            if analysis.activity:
                details.append(f"当前活动：{analysis.activity}")
            if analysis.topic:
                details.append(f"页面主题：{analysis.topic}")
            if analysis.change_summary:
                details.append(f"变化摘要：{analysis.change_summary}")
            details.append(
                f"当前时间：{build_time_context('zh-CN')}"
            )
        if self._try_start_proactive_event("\n".join(details), event_key="screen:" + event_key):
            self._last_spoken_screen_event = event_key

    def start_thread(
        self,
        text: str,
        role: str = "user",
        t: bool = False,
        source: str = "typed",
    ) -> None:
        del t  # Kept for compatibility with previous call sites.
        if self._closing or (getattr(self, "_dnd_enabled", False) and self.settings.character.profile_id == "nene"):
            return
        clean_text = text.strip()
        if not clean_text:
            return
        user_at = datetime.now().astimezone().isoformat(timespec="seconds")
        request_started = time.monotonic()

        # Head pats call this entry point directly. Like idle/screen events,
        # they are opportunistic: skip them while a reply is being prepared
        # or spoken instead of cancelling requested speech or queuing a reply.
        if role == "system" and self._proactive_event_blocked():
            return

        interaction = getattr(self, "interaction_started", None)
        if interaction is not None:
            interaction.emit()

        self._cancel_active_jobs(include_vision=(role == "user"))
        if role == "user":
            self._proactive_awaiting_user = False
            self._pause_until_user = False
            self._idle_resume_at = time.monotonic()
        self._generation += 1
        generation = self._generation
        event_context = clean_text if role == "system" else None
        screen_memory = (
            self.screen_memory_store.prompt_text()
            if self.settings.vision.enabled
            else None
        )
        history_context = self.history_store.context_messages(self.history)
        conversation_memory = self.history_store.conversation_context(
            clean_text, history_context,
        )
        character_context = None
        continuity = getattr(self, "continuity_store", None)
        interaction = getattr(self.settings, "interaction", None)
        if (continuity is not None and self.settings.character.profile_id == "nene"
                and getattr(interaction, "continuity_enabled", True)):
            character_context = continuity.context(clean_text)
        if self.settings.character.profile_id == "nene":
            from aipet.core.nene_persona import scene_context
            scene_evidence = scene_context(clean_text)
            if scene_evidence:
                character_context = "\n".join(part for part in (character_context, scene_evidence) if part)
        worker = ConversationWorker(
            self.settings.model_copy(deep=True),
            history_context,
            clean_text if role == "user" else "",
            event_context=event_context,
            screen_memory=screen_memory,
            user_source=source if role == "user" else "typed",
            conversation_memory=conversation_memory or None,
            character_context=character_context,
            user_at=user_at if role == "user" else None,
            parent=self,
        )
        self._workers[generation] = worker
        worker.result_ready.connect(
            lambda result, current=generation: self._on_reply(current, result)
        )
        worker.stage_changed.connect(
            lambda stage, current=generation: self._on_worker_stage(current, stage)
        )
        worker.error.connect(
            lambda message, current=generation: self._on_worker_error(
                current,
                message,
            )
        )
        worker.warning.connect(
            lambda message, current=generation: self.notification.emit("语音合成", message)
            if not self._closing and not self._dnd_enabled and current == self._generation else None
        )
        worker.finished.connect(
            lambda current=generation, current_worker=worker: (
                self._finish_worker(current, current_worker)
            )
        )
        self._response_target_elapsed = False
        self._start_thinking_animation()
        target_timer = getattr(self, "response_target_timer", None)
        if target_timer is not None:
            important = bool(continuity and (continuity.relationship_status == "separated"
                             or any(word in continuity.mood.casefold() for word in (
                                 "生气", "失望", "不满", "担心", "着急", "难过", "委屈",
                                 "angry", "anger", "disappoint", "hurt", "unhappy", "worri", "anxious",
                                 "怒", "悲し", "不安", "寂し", "がっかり"))))
            important = important or len(clean_text) > 240
            target = getattr(interaction, "important_response_target_seconds" if important else "response_target_seconds", 30 if important else 10)
            self._response_waiting_generation = generation
            self._response_target_elapsed = False
            if target > 0:
                target_timer.start(max(1, int(target * 1000 - (time.monotonic() - request_started) * 1000)))
        worker.start()

    def _on_response_target_reached(self) -> None:
        if (self._closing or self._dnd_enabled
                or self._response_waiting_generation != self._generation
                or self._generation not in self._workers):
            return
        self._response_target_elapsed = True
        self._thinking_label = self._localized(
            "Still processing; you can cancel from the tray", "仍在处理，可从托盘取消本次回复"
        )
        self._thinking_step()

    def settings_for_persistence(self) -> AppSettings:
        settings = self.settings.model_copy(deep=True)
        if getattr(self, "_test_mode", False) and self._formal_session is not None:
            settings.character.outfit = self._formal_session[4]
        return settings

    def cancel_conversation(self) -> None:
        self._cancel_active_jobs(include_vision=True)
        self._clear_dialogue()

    def _finish_worker(
        self,
        generation: int,
        worker: ConversationWorker,
    ) -> None:
        self._workers.pop(generation, None)
        worker.deleteLater()

    def _on_worker_stage(self, generation: int, stage: str) -> None:
        if self._closing or getattr(self, "_dnd_enabled", False) or generation != self._generation or stage != "voice":
            return
        self._thinking_label = self._localized("Generating voice", "正在生成语音")
        if getattr(self, "_response_target_elapsed", False):
            self._thinking_label += self._localized("; still processing", "，仍在处理，可从托盘取消")
        self._thinking_dot_count = 0
        self._thinking_step()

    def _on_worker_error(self, generation: int, message: str) -> None:
        if self._closing or getattr(self, "_dnd_enabled", False) or generation != self._generation:
            return
        self._stop_thinking_animation()
        Murasame._restore_avatar_face(self)
        timer = getattr(self, "response_target_timer", None)
        if timer is not None:
            timer.stop()
        self.show_text(
            message,
            typing=False,
            speaker_name=self._localized("System", "系统"),
        )
        self.notification.emit("模型请求失败", message)

    def _on_reply(
        self,
        generation: int,
        result: ConversationResult,
    ) -> None:
        if (
            self._closing or generation != self._generation
            or getattr(self, "_dnd_enabled", False)
            or (result.profile_id and result.profile_id != self.settings.character.profile_id)
        ):
            self._remove_audio_files(result)
            return

        self._stop_thinking_animation()
        timer = getattr(self, "response_target_timer", None)
        if timer is not None:
            timer.stop()
        if not result.is_user_message and not getattr(result.reply, "should_speak", True):
            self._proactive_awaiting_user = True
            self._remove_audio_files(result)
            Murasame._restore_avatar_face(self)
            return
        if not result.is_user_message and self.settings.character.profile_id == "nene":
            self._proactive_awaiting_user = True
        nene = self.settings.character.profile_id == "nene"
        if not Murasame._commit_continuity(self, result, commit=not nene):
            self._remove_audio_files(result)
            self._on_worker_error(generation, self._localized(
                "The relationship change could not be saved; this response was not delivered.",
                "关系变化未能核验或保存，本次回复未播出。",
            ))
            return
        outfit = result.reply.outfit or self.settings.character.outfit
        self.settings.character.outfit = outfit
        if nene:
            self._delivery_result = result
            self._delivery_count = 0
        else:
            Murasame._record_delivered_reply(self, result)
        self._playback_result = result
        self._playback_index = 0
        self._play_next_sentence()

    def _record_delivered_reply(self, result: ConversationResult, *, partial: bool = False) -> None:
        separate_proactive = self.history_store.separate_proactive is True
        reply_content = result.reply.model_dump_json(exclude_none=True)
        episode_metadata = {}
        if (
            separate_proactive and result.is_user_message
            and result.profile_id == "nene" and result.episode_id and result.user_at
        ):
            try:
                episode_id = self.history_store.remember_exchange(
                    episode_id=result.episode_id, user_at=result.user_at,
                    completed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                    user_source=result.user_source, user_text=result.user_text,
                    assistant_reply=reply_content, profile_id=result.profile_id,
                )
            except ValueError:
                # Invalid metadata must not turn a delivered reply into a
                # fabricated episode or interrupt the visible conversation.
                logger.warning("长期对话记录元数据无效，本轮未加入长期记忆")
            else:
                episode_metadata = {
                    "episode_id": episode_id, "user_at": result.user_at,
                    "profile_id": result.profile_id,
                }
        if result.is_user_message:
            if separate_proactive:
                pending = self.history_store.pop_proactive()
                if pending is not None:
                    self.history.append(pending)
            user_message = {
                "role": "user",
                "content": result.user_text,
                **episode_metadata,
            }
            if result.user_source == "voice":
                user_message["source"] = "voice"
            self.history.append(user_message)
        assistant_message = {
            "role": "assistant",
            "content": reply_content,
            **episode_metadata,
        }
        if partial:
            assistant_message["delivery"] = "interrupted"
        if separate_proactive and not result.is_user_message:
            self.history_store.record_proactive(
                assistant_message["content"], result.event_context or ""
            )
        else:
            self.history.append(assistant_message)
        self.history = self.history[-self.settings.history_limit :]
        self.history_store.save(self.history)

    def _mark_sentence_delivered(self) -> None:
        pending = getattr(self, "_delivery_result", None)
        if pending is None or pending is not getattr(self, "_playback_result", None):
            return
        index = getattr(self, "_playback_index", 0)
        if not 0 < index <= len(pending.reply.sentences):
            return
        expected = self._wrap_dialogue(pending.reply.sentences[index - 1].text(self.settings.ui_language))
        if self.full_text == expected and self.typing_index >= len(expected):
            self._delivery_count = max(getattr(self, "_delivery_count", 0), index)
            if self._delivery_count == len(pending.reply.sentences):
                Murasame._finish_delivery(self)

    def _finish_delivery(self, *, interrupted: bool = False) -> None:
        result = getattr(self, "_delivery_result", None)
        if result is None:
            return
        count = min(getattr(self, "_delivery_count", 0), len(result.reply.sentences))
        if not interrupted and count < len(result.reply.sentences):
            return
        self._delivery_result = None  # Commit at most once, including reentrant UI events.
        if count == 0:
            return
        partial = count < len(result.reply.sentences)
        if partial:
            prefix = result.reply.model_copy(update={
                "sentences": result.reply.sentences[:count], "continuity": None,
                "continuity_review_approved": False, "outfit": None,
            })
            result = replace(result, reply=prefix)
        saved = Murasame._commit_continuity(self, result)
        self._avatar_last_emotion = result.reply.sentences[-1].emotion
        Murasame._sync_avatar_mood(self)
        try:
            Murasame._record_delivered_reply(self, result, partial=partial)
        except (OSError, ValueError):
            logger.warning("已交付的对话记录未能写入本地存储", exc_info=False)
            self.notification.emit("记忆保存失败", "本轮内容已显示，但记忆未能保存，请检查本地存储。")
        if not result.is_user_message:
            continuity = getattr(self, "continuity_store", None)
            try:
                context = json.loads(result.event_context or "")
                reason = context.get("pending_reason") if isinstance(context, dict) else None
                if continuity is not None and isinstance(reason, str):
                    continuity.mark_proactive(reason)
            except (ValueError, TypeError, OSError):
                logger.warning("主动事项标记未保存", exc_info=False)
        if not saved:
            # Content was already completely displayed. Never call it unsaid.
            logger.warning("已交付的关系回复未能保存连续状态")
            self.notification.emit("状态保存失败", "本轮内容已显示，但关系状态未能保存，请检查本地存储。")

    def _commit_continuity(self, result: ConversationResult, *, commit: bool = True) -> bool:
        update = getattr(result.reply, "continuity", None)
        if update is None or not result.is_user_message or result.profile_id != "nene":
            return True
        major = update.relationship_action != "none"
        store = getattr(self, "continuity_store", None)
        if (store is None or not result.episode_id or not result.user_at
                or not getattr(getattr(self.settings, "interaction", None), "continuity_enabled", True)):
            return not major
        known = getattr(result, "user_evidence", ())
        approved = getattr(result.reply, "continuity_review_approved", False)
        if major and (not approved or not store.validate_relationship(
                update, result.user_text, known_user_evidence=known, episode_id=result.episode_id)):
            return False
        if not commit:
            return not (major and store.load_error)
        try:
            store.apply(update, user_text=result.user_text, episode_id=result.episode_id,
                        known_user_evidence=known, review_approved=approved)
        except (ValueError, OSError):
            logger.warning("本轮连续状态未提交", exc_info=False)
            return not major
        if major and store.relationship_status != ("separated" if update.relationship_action == "end" else "lovers"):
            return False
        try:
            self.screen_memory_store.confirm(update.screen_updates, user_text=result.user_text,
                                             episode_id=result.episode_id)
        except (ValueError, OSError):
            logger.warning("本轮屏幕观察确认未提交", exc_info=False)
        return True

    def _play_next_sentence(self) -> None:
        if self._closing or getattr(self, "_dnd_enabled", False):
            return
        if getattr(self, "_audio_clock_active", False):
            self.playback_timer.start(100)
            return
        result = self._playback_result
        if (getattr(self, "_delivery_result", None) is result and result is not None
                and getattr(self, "_delivery_count", 0) < self._playback_index):
            self.playback_timer.start(40)
            return
        if result is None or self._playback_index >= len(result.reply.sentences):
            if result is not None:
                self._remove_audio_files(result)
            self._playback_result = None
            self._sound = None
            if self._viewerex is not None:
                Murasame._restore_avatar_face(self)
                if result is not None and not self.input_mode:
                    self._clear_dialogue()
            return

        index = self._playback_index
        self._playback_index += 1
        sentence = result.reply.sentences[index]
        portrait = sentence.portrait or self.settings.character.portrait
        outfit = self.settings.character.outfit
        self.update_portrait(
            layers_for(portrait, sentence.emotion, outfit),
            portrait,
            outfit,
        )
        visible_text = sentence.text(self.settings.ui_language)
        self.show_text(visible_text, typing=True)

        audio_path = result.audio_paths[index] if index < len(result.audio_paths) else None
        audio_duration = self._audio_duration_milliseconds(audio_path)
        delay = max(40 * len(visible_text) + 800, audio_duration + 400)
        if (audio_path is not None and audio_duration > 0
                and getattr(self.settings.character, "profile_id", "") == "nene"
                and Murasame._start_clock_audio(self, audio_path, audio_duration / 1000, sentence.emotion)):
            return
        if self._viewerex is not None:
            if Murasame._avatar_performance_available(self):
                self._viewerex.play_speech(sentence.emotion,
                    audio_path if audio_duration > 0 else None,
                    duration_seconds=delay / 1000)
            else:
                self._viewerex.set_emotion(sentence.emotion)
        if audio_path is not None and audio_path.exists() and audio_duration > 0:
            self._sound = QSound(str(audio_path), self)
            self._sound.play()
            logger.info("语音播放开始 | duration_ms=%d | 音频=%s", audio_duration, audio_path)
        else:
            logger.info("此句无可播放语音 | sentence_index=%d", index)
        self.playback_timer.start(int(delay))

    def _start_clock_audio(self, path: Path, duration: float, emotion: str) -> bool:
        """One processing clock drives captions and finite Viewer requests.

        Viewer and the physical speaker still have unmeasured output latency.
        Unsupported devices retain the original QSound path explicitly.
        """
        player = AudioPlayback(self)
        self._audio_clock_generation = getattr(self, "_audio_clock_generation", 0) + 1
        epoch = self._audio_clock_generation
        self._audio_clock_player = player
        self._audio_clock_active = True
        self._sound = player
        preparing = {"value": True}
        prepared = False
        viewer = getattr(self, "_viewerex", None)
        if Murasame._avatar_performance_available(self):
            prepare = getattr(viewer, "prepare_timeline_speech", None)
            if callable(prepare):
                prepared = prepare(emotion, path, duration + .5) is True

        def current():
            return (not self._closing and not getattr(self, "_dnd_enabled", False)
                and epoch == self._audio_clock_generation and self._audio_clock_player is player)

        def position(seconds):
            if not current() or not self._audio_clock_active:
                return
            fraction = min(1.0, max(0.0, float(seconds) / max(duration, .001)))
            self.typing_index = max(self.typing_index, int(len(self.full_text) * fraction))
            self.display_text = self.typing_prefix + self.full_text[:self.typing_index]
            Murasame._mark_sentence_delivered(self)
            self.update()
            if prepared:
                viewer.sync_speech_position(seconds)

        def started():
            if not current():
                return
            self.typing_timer.stop()
            if prepared:
                viewer.sync_speech_position(0.0)
            elif viewer is not None:
                if Murasame._avatar_performance_available(self):
                    viewer.play_speech(emotion, path, duration_seconds=duration + .5)
                else:
                    viewer.set_emotion(emotion)

        def finished():
            if not current():
                return
            position(duration)
            self._audio_clock_active = False
            self._audio_clock_player = None
            self._sound = None
            if prepared:
                viewer.sync_speech_position(duration, force=True)
            player.deleteLater()
            self.playback_timer.start(800)

        def failed(message):
            if not current() or preparing["value"]:
                return
            self._audio_clock_active = False
            self._audio_clock_player = None
            self._sound = None
            if prepared:
                viewer.finish_speech()
            player.deleteLater()
            self.typing_timer.start()
            self.playback_timer.start(max(800, (len(self.full_text) - self.typing_index) * 40 + 800))
            logger.warning("音频输出中断 | 文字继续显示")
            self.notification.emit("语音播放", "音频输出中断，文字将继续显示。")

        player.started.connect(started)
        player.position_changed.connect(position)
        player.finished.connect(finished)
        player.failed.connect(failed)
        accepted = player.play(path) is True
        preparing["value"] = False
        if accepted:
            if self._audio_clock_active:
                self.playback_timer.start(100)
            logger.info("语音播放开始 | clock=processed_audio | duration_ms=%d", int(duration * 1000))
            return True
        self._audio_clock_active = False
        self._audio_clock_player = None
        self._sound = None
        player.deleteLater()
        if prepared:
            viewer.finish_speech()
        # A backend can emit started and then fail synchronously from play().
        # started already stopped typewriter updates, so restore them for the
        # QSound fallback; otherwise its delivery gate would wait forever.
        if (not self._closing and not getattr(self, "_dnd_enabled", False)
                and self.typing_index < len(self.full_text)):
            self.typing_timer.start()
        logger.info("音频设备不支持处理时钟路径，回退原播放器与估计时间轴")
        return False

    @staticmethod
    def _audio_duration_milliseconds(path: Path | None) -> int:
        if path is None or not path.exists():
            return 0
        try:
            with wave.open(str(path), "rb") as audio:
                return int(audio.getnframes() / audio.getframerate() * 1000)
        except (OSError, wave.Error, ZeroDivisionError):
            return 0

    def _cancel_active_jobs(self, *, include_vision: bool = False, preserve_voice_input: bool = False) -> None:
        self._generation += 1
        if include_vision and not preserve_voice_input:
            self._voice_input_epoch = getattr(self, "_voice_input_epoch", 0) + 1
            self._voice_input_pending = False
        timer = getattr(self, "response_target_timer", None)
        if timer is not None:
            timer.stop()
        status_timer = getattr(self, "status_clear_timer", None)
        if status_timer is not None:
            status_timer.stop()
        self._response_waiting_generation = None
        for worker in self._workers.values():
            worker.cancel()
        if include_vision and self._vision_worker is not None:
            self._vision_worker.cancel()
        self._stop_playback()

    def _stop_playback(self) -> None:
        clock_player = getattr(self, "_audio_clock_player", None)
        self._audio_clock_generation = getattr(self, "_audio_clock_generation", 0) + 1
        self._audio_clock_active = False
        self._audio_clock_player = None
        Murasame._finish_delivery(self, interrupted=True)
        self.playback_timer.stop()
        self.typing_timer.stop()
        self._stop_thinking_animation()
        if self._sound is not None:
            self._sound.stop()
            self._sound = None
        if clock_player is not None:
            clock_player.deleteLater()
        if self._playback_result is not None:
            self._remove_audio_files(self._playback_result)
            self._playback_result = None
        if self._viewerex is not None:
            Murasame._restore_avatar_face(self)

    @staticmethod
    def _remove_audio_files(result: ConversationResult) -> None:
        for path in result.audio_paths:
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def update_portrait(
        self,
        layers: list[int],
        portrait: str | None = None,
        outfit: str | None = None,
    ) -> None:
        if self._viewerex is not None:
            self._current_layers = list(layers)
            self._current_portrait = portrait or self.settings.character.portrait
            self._current_outfit = outfit or self.settings.character.outfit
            if self._viewer_layout is None:
                self._prepare_live2d_overlay()
                self.move(self._viewer_layout.geometry.topLeft())
                placeholder = QPixmap(self.size())
                placeholder.fill(Qt.transparent)
                self.setPixmap(placeholder)
            self.update()
            return
        previous_geometry = (
            self.geometry()
            if self._source_portrait_pixmap is not None
            else None
        )
        screen = (
            self._screen_at_current_position()
            if previous_geometry is not None
            else self._configured_screen()
        )
        portrait = portrait or self.settings.character.portrait
        outfit = outfit or self.settings.character.outfit
        self._current_layers = list(layers)
        self._current_portrait = portrait
        self._current_outfit = outfit
        target = f"ムラサメ{portrait}"
        bgra_image = generate_fgimage(target, layers)
        rgba_image = cv2.cvtColor(bgra_image, cv2.COLOR_BGRA2RGBA)
        height, width, channels = rgba_image.shape
        image = QImage(
            rgba_image.data,
            width,
            height,
            channels * width,
            QImage.Format_RGBA8888,
        ).copy()
        self._source_portrait_pixmap = QPixmap.fromImage(image)
        pixmap = self._scale_portrait_pixmap(
            self._source_portrait_pixmap,
            screen,
        )
        self.setPixmap(pixmap)
        self.resize(pixmap.size())
        if previous_geometry is not None:
            x = previous_geometry.center().x() - pixmap.width() // 2
            y = previous_geometry.bottom() - pixmap.height() + 1
            self.move(x, y)
        self.update()

    def _configured_screen(self) -> QScreen | None:
        screens = QGuiApplication.screens()
        index = self.settings.display.screen_index
        return (
            screens[index]
            if 0 <= index < len(screens)
            else QGuiApplication.primaryScreen()
        )

    def _scale_portrait_pixmap(
        self,
        pixmap: QPixmap,
        screen: QScreen | None = None,
    ) -> QPixmap:
        screen = screen or self._configured_screen()
        available_height = (
            screen.availableGeometry().height() if screen is not None else 0
        )
        target_height = self._target_portrait_height(
            pixmap.height(),
            available_height,
            self.settings.display.portrait_screen_ratio,
        )
        self._current_scale = target_height / max(1, pixmap.height())
        self._active_screen_key = self._screen_key(screen)
        self._update_text_scaling()
        return pixmap.scaledToHeight(target_height, Qt.SmoothTransformation)

    @staticmethod
    def _target_portrait_height(
        source_height: int,
        available_height: int,
        ratio: float,
    ) -> int:
        if available_height <= 0:
            return max(1, source_height)
        return max(1, int(available_height * ratio))

    @staticmethod
    def _screen_key(screen: QScreen | None) -> tuple[object, ...] | None:
        if screen is None:
            return None
        geometry = screen.availableGeometry()
        return (
            screen.name(),
            geometry.x(),
            geometry.y(),
            geometry.width(),
            geometry.height(),
        )

    def _screen_at_current_position(self) -> QScreen | None:
        screen = QGuiApplication.screenAt(self.frameGeometry().center())
        if screen is not None:
            return screen
        handle = self.windowHandle()
        if handle is not None and handle.screen() is not None:
            return handle.screen()
        return self._configured_screen()

    def remember_window_position(self) -> None:
        screen = self._screen_at_current_position()
        if screen is None:
            return

        available = screen.availableGeometry()
        display = self.settings.display
        display.screen_name = screen.name()
        display.window_x = self.x() - available.x()
        display.window_y = self.y() - available.y()

        screen_key = self._screen_key(screen)
        for index, candidate in enumerate(QGuiApplication.screens()):
            if candidate is screen or self._screen_key(candidate) == screen_key:
                display.screen_index = index
                break

    def _adapt_to_current_screen(self) -> None:
        if self._viewerex is not None:
            # A calibrated ViewerEX model has its own scale. Do not resize or
            # reposition it merely because the Qt overlay crossed a monitor.
            self.remember_window_position()
            return
        source = self._source_portrait_pixmap
        screen = self._screen_at_current_position()
        if (
            source is None
            or source.isNull()
            or screen is None
            or self._screen_key(screen) == self._active_screen_key
        ):
            return

        old_geometry = self.geometry()
        scaled = self._scale_portrait_pixmap(source, screen)
        self.setPixmap(scaled)
        self.resize(scaled.size())

        x = old_geometry.center().x() - scaled.width() // 2
        y = old_geometry.bottom() - scaled.height() + 1
        self.move(x, y)

        screens = QGuiApplication.screens()
        screen_key = self._screen_key(screen)
        for index, candidate in enumerate(screens):
            if candidate is screen or self._screen_key(candidate) == screen_key:
                self.settings.display.screen_index = index
                break
        self.update()

    def _update_text_scaling(self) -> None:
        scale = max(self._current_scale, 0.1)
        font_size = max(10, round(self._base_font_size * scale))
        self.text_font = QFont(self._font_family, font_size)
        self.text_x_offset = max(
            10,
            round(self._base_text_x_offset * scale),
        )
        self.text_y_offset = min(
            -10,
            round(self._base_text_y_offset * scale),
        )
        self.border_size = max(1, round(self._base_border_size * scale))

    def show_text(
        self,
        text: str,
        typing: bool = True,
        speaker_name: str | None = None,
    ) -> None:
        if self._closing or (getattr(self, "_dnd_enabled", False) and self.settings.character.profile_id == "nene"):
            return
        self._stop_thinking_animation()
        status_timer = getattr(self, "status_clear_timer", None)
        if status_timer is not None:
            status_timer.stop()
            if speaker_name in {"系统", "System"}:
                status_timer.start(8_000)
        self.full_text = self._wrap_dialogue(text)
        self.typing_prefix = self._speaker_prefix(speaker_name)
        self.typing_index = 0
        self.typing_timer.stop()
        if typing:
            self.display_text = self.typing_prefix
            self.typing_timer.start()
        else:
            self.display_text = self.typing_prefix + self.full_text
            self.update()

    def _start_thinking_animation(self) -> None:
        Murasame._avatar_activity(self, "thinking")
        status_timer = getattr(self, "status_clear_timer", None)
        if status_timer is not None:
            status_timer.stop()
        self.typing_timer.stop()
        self._thinking_label = ""
        self._thinking_dot_count = 0
        self._thinking_step()
        self.thinking_timer.start()

    def _stop_thinking_animation(self) -> None:
        self.thinking_timer.stop()

    def _thinking_step(self) -> None:
        if getattr(self, "_closing", False) or getattr(self, "_dnd_enabled", False):
            return
        self._thinking_dot_count = self._thinking_dot_count % 6 + 1
        self.full_text = self._thinking_label + "." * self._thinking_dot_count
        self.typing_prefix = (self._speaker_prefix(self._localized("Status", "状态"))
                              if getattr(self, "_response_target_elapsed", False)
                              else self._speaker_prefix())
        self.display_text = self.typing_prefix + self.full_text
        self.update()

    def _typing_step(self) -> None:
        if getattr(self, "_closing", False) or getattr(self, "_dnd_enabled", False):
            self.typing_timer.stop()
            return
        if getattr(self, "_audio_clock_active", False):
            self.typing_timer.stop()
            return
        if self.typing_index >= len(self.full_text):
            self.typing_timer.stop()
            Murasame._mark_sentence_delivered(self)
            return
        self.typing_index += 1
        self.display_text = (
            self.typing_prefix + self.full_text[: self.typing_index]
        )
        Murasame._mark_sentence_delivered(self)
        self.update()

    def _clear_dialogue(self) -> None:
        status_timer = getattr(self, "status_clear_timer", None)
        if status_timer is not None:
            status_timer.stop()
        self.typing_timer.stop()
        self.full_text = ""
        self.display_text = ""
        self.typing_prefix = ""
        self.typing_index = 0
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._viewer_layout is not None:
            # Windows layered windows pass alpha-zero pixels through to the
            # desktop. Alpha 1 preserves the original character hit surface
            # while the actual model remains visible in the window underneath.
            hit_painter = QPainter(self)
            hit_painter.fillRect(self._viewer_layout.body_rect, QColor(0, 0, 0, 1))
            if self.display_text:
                # A subtitle is a second handle for the same model/overlay
                # drag. Keep gaps between glyphs clickable, and release this
                # area back to the desktop as soon as the subtitle disappears.
                hit_painter.fillRect(self._dialogue_rect(), QColor(0, 0, 0, 1))
            if self._viewerex.config.debug_bbox:
                hit_painter.setPen(QColor(255, 0, 255, 180))
                hit_painter.drawRect(self._viewer_layout.body_rect.adjusted(0, 0, -1, -1))
            hit_painter.end()
        if not self.display_text:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self.text_font)
        text_rect = self._dialogue_rect()
        alignment = (
            Qt.AlignLeft | Qt.AlignBottom
            if "\n" in self.display_text
            else Qt.AlignHCenter | Qt.AlignBottom
        )
        painter.setPen(QColor(44, 22, 28))
        for dx, dy in [
            (-self.border_size, 0),
            (self.border_size, 0),
            (0, -self.border_size),
            (0, self.border_size),
            (self.border_size, -self.border_size),
            (self.border_size, self.border_size),
            (-self.border_size, -self.border_size),
            (-self.border_size, self.border_size),
        ]:
            painter.drawText(
                text_rect.translated(dx, dy),
                alignment,
                self.display_text,
            )
        painter.setPen(Qt.white)
        painter.drawText(text_rect, alignment, self.display_text)
        painter.end()

    def begin_text_input(self) -> None:
        """Enter the original inline editor from Qt or a ViewerEX model tap."""
        if self._closing or (getattr(self, "_dnd_enabled", False) and self.settings.character.profile_id == "nene"):
            return
        interaction = getattr(self, "interaction_started", None)
        if interaction is not None:
            interaction.emit()
        logger.info(
            "输入对话入口 | renderer=%s | already_editing=%s",
            "viewerex" if self._viewerex is not None else "portrait", self.input_mode,
        )
        if self._viewerex is not None and self.input_mode:
            # A repeated/late model tap only restores focus. In particular it
            # must not discard a draft or an active Chinese IME composition.
            self.raise_()
            self.activateWindow()
            self.setFocus(Qt.MouseFocusReason)
            self.update()
            return
        self._cancel_active_jobs(include_vision=True)
        self.touch_head = False
        self.head_press_x = None
        self.input_mode = True
        Murasame._avatar_activity(self, "listening")
        self.input_buffer = ""
        self.preedit_text = ""
        self.display_text = self._input_display("...")
        if self._viewerex is not None:
            self.raise_()
            self.activateWindow()
        self.setFocus(Qt.MouseFocusReason)
        self.update()

    def mousePressEvent(self, event) -> None:
        local_y = event.y()
        if self._viewer_layout is not None:
            body = self._viewer_layout.body_rect
            on_body = body.contains(event.pos())
            on_subtitle_drag = (
                event.button() == Qt.MiddleButton and bool(self.display_text)
                and self._dialogue_rect().contains(event.pos())
            )
            if not on_body and not on_subtitle_drag:
                return
            local_y = (event.y() - body.y()) * 640 / max(1, body.height())
        if event.button() == Qt.LeftButton:
            if local_y < 150:
                logger.info("Qt 点击命中 | area=head")
                self.touch_head = True
                self.head_press_x = event.x()
                self.setCursor(Qt.OpenHandCursor)
            elif self._viewer_layout is not None or local_y > 280:
                logger.info("Qt 点击命中 | area=body")
                self.begin_text_input()
            else:
                self.touch_head = False
                self.head_press_x = None
        elif event.button() == Qt.MiddleButton:
            if self._viewerex is not None and not self._viewerex.begin_drag(self.pos()):
                return
            self.drag_offset = event.pos()
            if self._viewerex is not None:
                # Local mouse coordinates change as the overlay itself moves.
                # Use one desktop anchor for the entire drag so subtitles and
                # Viewer receive exactly the same displacement on every move.
                self._viewer_drag_origin = (event.globalPos(), self.pos())
                self.touch_head = False
                self.head_press_x = None
            self.setCursor(Qt.SizeAllCursor)

    def mouseMoveEvent(self, event) -> None:
        if self.touch_head and self.head_press_x is not None:
            if abs(event.x() - self.head_press_x) > 50:
                head_pat = (
                    self._localized(
                        "Master gently patted your head.",
                        "主人摸了摸你的头。",
                    )
                    if self.settings.character.profile_id == "murasame"
                    else self._localized(
                        f"The user gently patted {self.pet_name}'s head.",
                        f"用户轻轻摸了摸{self.pet_name}的头。",
                    )
                )
                self.start_thread(
                    head_pat,
                    role="system",
                )
                self.touch_head = False
        if self.drag_offset is not None and event.buttons() & Qt.MiddleButton:
            if self._viewerex is not None and self._viewer_drag_origin is not None:
                press_global, overlay_start = self._viewer_drag_origin
                target = overlay_start + event.globalPos() - press_global
            else:
                target = self.pos() + event.pos() - self.drag_offset
            if self._viewerex is None or self._viewerex.drag_to(target):
                self.move(target)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.touch_head = False
            self.head_press_x = None
            self.setCursor(Qt.ArrowCursor)
        elif event.button() == Qt.MiddleButton:
            if self._viewerex is not None:
                self._viewerex.end_drag()
            self.drag_offset = None
            self._viewer_drag_origin = None
            self.setCursor(Qt.ArrowCursor)
            self._adapt_to_current_screen()
            self.remember_window_position()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if (
            hasattr(self, "_screen_resize_timer")
            and self.drag_offset is None
        ):
            self._screen_resize_timer.start()

    def inputMethodQuery(self, query):
        if query in (Qt.ImMicroFocus, Qt.ImCursorRectangle):
            if self._viewer_layout is not None:
                text_rect = self._dialogue_rect()
            else:
                rect = self.rect()
                text_rect = QRect(
                    rect.x() + self.text_x_offset,
                    rect.y() + self.text_y_offset,
                    max(1, rect.width() - 2 * self.text_x_offset),
                    max(1, rect.height() // 2 - self.text_y_offset),
                )
            font_metrics = QFontMetrics(self.text_font)
            last_line = (self.display_text or "").split("\n")[-1]
            x = text_rect.x() + min(
                max(0, font_metrics.horizontalAdvance(last_line)),
                max(1, text_rect.width() - 1),
            )
            caret = QRect(
                int(x),
                text_rect.bottom() - font_metrics.height(),
                1,
                font_metrics.height(),
            )
            return caret.intersected(self.rect().adjusted(0, 0, -1, -1))
        return super().inputMethodQuery(query)

    def inputMethodEvent(self, event) -> None:
        if not self.input_mode:
            super().inputMethodEvent(event)
            return
        self.input_buffer += event.commitString()
        self.preedit_text = event.preeditString()
        self._show_input_buffer()

    def keyPressEvent(self, event) -> None:
        if not self.input_mode:
            super().keyPressEvent(event)
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            text = self.input_buffer.strip()
            self.input_mode = False
            self.preedit_text = ""
            if text:
                self.start_thread(text, role="user")
            else:
                Murasame._avatar_activity(self, "idle")
                prompt = (
                    self._localized(
                        "What did you say, Master?",
                        "主人，你说什么？",
                    )
                    if self.settings.character.profile_id == "murasame"
                    else self._localized(
                        "What would you like to say?",
                        "想和我说点什么？",
                    )
                )
                self.show_text(prompt)
            return
        if event.key() == Qt.Key_Escape:
            self.input_mode = False
            Murasame._avatar_activity(self, "idle")
            self.preedit_text = ""
            if self._viewerex is not None:
                self._clear_dialogue()
            else:
                self.show_text("……？", typing=False)
            return
        if event.key() == Qt.Key_Backspace and not self.preedit_text:
            self.input_buffer = self.input_buffer[:-1]
            self._show_input_buffer()
            return
        text = event.text()
        if text and not self.preedit_text:
            self.input_buffer += text
            self._show_input_buffer()

    def _show_input_buffer(self) -> None:
        content = (
            self._wrap_dialogue(self.input_buffer + self.preedit_text)
            or "..."
        )
        self.display_text = self._input_display(content)
        self.update()

    def clear_history(self) -> None:
        if self._closing:
            return
        self._cancel_active_jobs(include_vision=True)
        self._reset_screen_observation()
        self.history.clear()
        self.history_store.clear()
        self.screen_memory_store.clear()
        continuity = getattr(self, "continuity_store", None)
        if continuity is not None:
            continuity.clear()
        self._avatar_last_emotion = "平静"
        Murasame._sync_avatar_mood(self)
        self._proactive_awaiting_user = False
        portrait = self.settings.character.portrait
        outfit = self.settings.character.outfit
        self.update_portrait(
            default_layers(portrait, outfit),
            portrait,
            outfit,
        )
        self.show_text(
            self._localized(
                "I've forgotten our previous conversation and screen events.",
                "已经忘掉之前的对话和屏幕事件了。",
            ),
            typing=False,
        )

    def begin_shutdown(self) -> None:
        """Quiesce immediately; the application keeps this owner until idle."""
        if self._closing:
            return
        self._closing = True
        self._topmost_timer.stop()
        self._screen_resize_timer.stop()
        self.screenshot_timer.stop()
        self.idle_timer.stop()
        self._cancel_active_jobs(include_vision=True)
        if self._viewerex is not None:
            self._viewerex.stop()

    def shutdown(self) -> None:
        self.begin_shutdown()
        self.history_store.save(self.history)

