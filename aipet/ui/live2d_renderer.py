"""Live2DViewerEX transport for the original Qt pet's transparent overlay.

ViewerEX renders the model. An explicit ``AIPET_VIEWER_MODEL_PATH`` optionally
loads an exported standard model3 into Nene's slot 1; without it, the existing
Viewer model is preserved. This adapter never changes costumes, guesses
expression IDs, or displays ViewerEX text bubbles. Dialogue, IME input, head
pats, and audio remain in ``Murasame``.

``AIPET_VIEWER_BBOX=x,y,width,height`` calibrates the character's desktop bounds
in Qt logical pixels. The original 2100,750,400,640 calibration is a 2560x1440
reference and follows the selected screen's size. Other visible explicit bounds
are used directly; an offscreen box fitting that reference is scaled, and all
boxes are constrained to the current screen. ``AIPET_VIEWER_POSITION=x,y`` is an
explicit ExAPI 13400 baseline (screen-centred X, upward Y); it is NOT inferred
from that bounding box. Without it, middle-button dragging is disabled.

By default startup, settings changes, and reconnects never reposition the model.
``AIPET_VIEWER_MANAGED_POSITION=1`` opts into a controlled position for slot 1:
after every registration ACK the model is placed at the configured baseline or
the most recent position reached by this session's middle-button drag. Position
notifications are not available: if the model is moved in ViewerEX externally,
reconnect or restart to restore the controlled position. This mode does not
change model scale, clothing, or any other slot.

ExAPI 13000 has no documented model-loaded acknowledgement. After sending it,
a cancellable 1.5-second Qt timer delays position/face commands and coalesces
the latest requested emotion. This is a settling delay, not proof that Viewer
has finished loading; actual model display still needs visual verification.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import QObject, QPoint, QRect, QTimer, QUrl, pyqtSignal
from PyQt5.QtNetwork import QAbstractSocket
from PyQt5.QtWebSockets import QWebSocket

from aipet.core.runtime_logging import get_logger
from aipet.core.config import get_cache_dir
from aipet.core.live2d_performance import compose_motion, motion_window


MOTION_NAMES = {
    "平静": "calm",
    "高兴": "happy",
    "害羞": "shy",
    "生气": "angry",
    "惊讶": "surprised",
    "着急": "worried",
}
MOTION_ROOT = Path(__file__).resolve().parents[2] / "assets" / "motions"
MODEL_LOAD_SETTLE_MS = 1500
PERFORMANCE_CLIP_SECONDS = 12.0
PERFORMANCE_REFRESH_MS = 10000
MOTION_CACHE_LIMIT = 64
MOTION_CACHE_RETAIN_SECONDS = 120.0
LOGGER = get_logger("live2d")


def validate_model3_path(path: Path) -> Path:
    """Check a local export's declared dependencies without loading its model."""
    path = path.expanduser().resolve()
    if not path.name.lower().endswith(".model3.json"):
        raise ValueError("模型入口必须是导出的 .model3.json，不能直接使用 .cmo3 工程")
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict) or manifest.get("Version") != 3:
        raise ValueError("模型入口不是标准 Version 3 model3 配置")
    references = manifest.get("FileReferences")
    if not isinstance(references, dict):
        raise ValueError("模型配置缺少 FileReferences")

    def require_file(value, label: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"模型资源引用无效：{label}")
        dependency = (path.parent / value).resolve()
        if not dependency.is_file() or dependency.stat().st_size == 0:
            raise ValueError(f"模型资源缺失或为空：{label} ({value})")

    require_file(references.get("Moc"), "Moc")
    textures = references.get("Textures")
    if not isinstance(textures, list) or not textures:
        raise ValueError("模型配置缺少 Textures")
    for index, texture in enumerate(textures):
        require_file(texture, f"Textures[{index}]")
    for key in ("Physics", "Pose", "DisplayInfo", "UserData", "MotionSync", "ParameterController"):
        if key in references:
            require_file(references[key], key)
    expressions = references.get("Expressions", [])
    if not isinstance(expressions, list):
        raise ValueError("模型 Expressions 配置无效")
    for expression in expressions:
        require_file(expression.get("File") if isinstance(expression, dict) else None, "Expressions.File")
    motions = references.get("Motions", {})
    if not isinstance(motions, dict):
        raise ValueError("模型 Motions 配置无效")
    for group, entries in motions.items():
        if not isinstance(entries, list):
            raise ValueError(f"模型动作组无效：{group}")
        for motion in entries:
            require_file(motion.get("File") if isinstance(motion, dict) else None, f"Motions.{group}.File")
            if "Sound" in motion:
                require_file(motion["Sound"], f"Motions.{group}.Sound")
    return path


def _numbers(value: str, count: int, name: str) -> tuple[float, ...]:
    try:
        result = tuple(float(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise ValueError(f"{name} must contain {count} comma-separated numbers") from exc
    if len(result) != count or not all(math.isfinite(number) for number in result):
        raise ValueError(f"{name} must contain {count} finite numbers")
    return result


@dataclass(frozen=True)
class OverlayLayout:
    geometry: QRect
    body_rect: QRect
    dialogue_rect: QRect


@dataclass(frozen=True)
class ViewerEXConfig:
    port_file: Path
    port: int = 10086
    slot: int = 1
    bbox: tuple[int, int, int, int] | None = None
    position: tuple[float, float] | None = None
    managed_position: bool = False
    overlay_width: int | None = None
    dialogue_height: int | None = None
    font_size: int = 26
    debug_bbox: bool = False
    model_path: Path | None = None

    @classmethod
    def from_environment(cls) -> "ViewerEXConfig":
        bbox_text = os.getenv("AIPET_VIEWER_BBOX", "").strip()
        bbox = (
            tuple(round(value) for value in _numbers(bbox_text, 4, "AIPET_VIEWER_BBOX"))
            if bbox_text else None
        )
        if bbox is not None and (bbox[2] < 40 or bbox[3] < 80):
            raise ValueError("AIPET_VIEWER_BBOX width/height must be at least 40/80")
        position_text = os.getenv("AIPET_VIEWER_POSITION", "").strip()
        position = (
            _numbers(position_text, 2, "AIPET_VIEWER_POSITION")
            if position_text else None
        )
        port = int(os.getenv("AIPET_VIEWER_PORT", "10086"))
        slot = int(os.getenv("AIPET_VIEWER_SLOT", "1"))
        model_text = os.getenv("AIPET_VIEWER_MODEL_PATH", "").strip()
        managed_position = os.getenv("AIPET_VIEWER_MANAGED_POSITION", "").lower() in {
            "1", "true", "yes", "on",
        }
        if not 1 <= port <= 65535 or not 0 <= slot <= 31:
            raise ValueError("ViewerEX port or model slot is outside its valid range")
        if managed_position and slot != 1:
            raise ValueError("Managed ViewerEX position is restricted to Nene's slot 1")
        if model_text and slot != 1:
            raise ValueError("Managed ViewerEX model loading is restricted to Nene's slot 1")
        width_text = os.getenv("AIPET_VIEWER_OVERLAY_WIDTH", "").strip()
        height_text = os.getenv("AIPET_VIEWER_DIALOGUE_HEIGHT", "").strip()
        width = int(width_text) if width_text else None
        height = int(height_text) if height_text else None
        font_size = int(os.getenv("AIPET_VIEWER_FONT_SIZE", "26"))
        if (width is not None and width < 100) or (height is not None and height < 80):
            raise ValueError("ViewerEX overlay width/dialogue height is too small")
        if not 10 <= font_size <= 72:
            raise ValueError("AIPET_VIEWER_FONT_SIZE must be between 10 and 72")
        return cls(
            port_file=Path(os.getenv(
                "AIPET_VIEWER_PORT_FILE",
                str(get_cache_dir() / "viewer-not-configured"),
            )),
            port=port,
            slot=slot,
            bbox=bbox,
            position=position,
            managed_position=managed_position,
            overlay_width=width,
            dialogue_height=height,
            font_size=font_size,
            debug_bbox=os.getenv("AIPET_VIEWER_DEBUG_BBOX", "").lower() in {"1", "true"},
            model_path=Path(model_text) if model_text else None,
        )

    def layout(self, screen: QRect) -> OverlayLayout:
        if screen.width() <= 0 or screen.height() <= 0:
            raise ValueError("The overlay needs a nonempty screen geometry")
        reference = QRect(0, 0, 2560, 1440)
        reference_bbox = (2100, 750, 400, 640)
        body = QRect(*(self.bbox or reference_bbox))
        use_reference = (
            self.bbox is None
            or self.bbox == reference_bbox
            or (not screen.contains(body) and reference.contains(body))
        )
        if use_reference:
            body = QRect(
                screen.x() + round(body.x() * screen.width() / reference.width()),
                screen.y() + round(body.y() * screen.height() / reference.height()),
                max(1, round(body.width() * screen.width() / reference.width())),
                max(1, round(body.height() * screen.height() / reference.height())),
            )
        # A stale/custom calibration must not produce an offscreen hit surface.
        # Clamping only affects Qt; it never sends a ViewerEX position command.
        body.setWidth(min(max(1, body.width()), screen.width()))
        body.setHeight(min(max(1, body.height()), screen.height()))
        body.moveLeft(max(screen.left(), min(body.left(), screen.right() - body.width() + 1)))
        body.moveTop(max(screen.top(), min(body.top(), screen.bottom() - body.height() + 1)))
        width = max(body.width(), self.overlay_width or round(screen.width() * 560 / 2560))
        width = min(max(min(100, screen.width()), width), screen.width())
        height = self.dialogue_height or max(1, round(screen.height() * 260 / 1440))
        x = body.center().x() - width // 2
        x = max(screen.x(), min(x, screen.x() + screen.width() - width))
        y = max(screen.y(), body.y() - height)
        geometry = QRect(x, y, width, body.bottom() - y + 1)
        local_body = body.translated(-x, -y)
        margin = min(16, max(0, width // 4))
        text_top = min(8, max(0, geometry.height() - 1))
        text_height = local_body.y() - text_top - 16
        if text_height <= 0:
            # Very large/upper-screen custom models leave no space above them.
            # Keep the original text renderer visible over the upper third.
            text_height = max(1, geometry.height() // 3)
        dialogue = QRect(
            margin, text_top,
            max(1, width - 2 * margin),
            min(text_height, max(1, geometry.height() - text_top)),
        )
        return OverlayLayout(geometry, local_body, dialogue)

    def discover_port(self) -> int:
        try:
            port = int(self.port_file.read_text(encoding="utf-8-sig").strip())
            if 1 <= port <= 65535:
                return port
        except (OSError, ValueError):
            pass
        return self.port


class ViewerEXRenderer(QObject):
    """Nene-only optional model loading, face motions and calibrated drags."""

    status_changed = pyqtSignal(bool, str)
    warning = pyqtSignal(str)
    model_tapped = pyqtSignal()

    def __init__(self, config: ViewerEXConfig, parent=None, *, socket=None):
        super().__init__(parent)
        self.config = config
        self.socket = socket if socket is not None else QWebSocket(parent=self)
        self.socket.connected.connect(self._connected)
        self.socket.disconnected.connect(self._disconnected)
        self.socket.textMessageReceived.connect(self._received)
        self.socket.error.connect(lambda _error: self._disconnected())
        self._serial = 0
        self._registered = False
        self._closed = False
        self._emotion: str | None = None
        self._desired_emotion: str | None = None
        self._base_emotion = "平静"
        self._base_strength = .65
        self._activity = "idle"
        self._attention = (0.0, 0.0)
        self._paused = False
        self._speech_active = False
        self._timeline_motion = None
        self._timeline_next_position = 0.0
        self._motion_seed = 0
        self._motion_directory: Path | None = None
        self._motion_files: list[tuple[Path, float]] = []
        self._last_motion_file: Path | None = None
        self._last_motion_sent_at = 0.0
        self._model_loading = False
        self._managed_model_valid = False
        self._model_load_timer: QTimer | None = None
        self._model_generation = 0
        self._position = config.position
        self._drag_start: QPoint | None = None
        self._drag_position: tuple[float, float] | None = None
        self._last_warning = ""
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(3000)
        self._reconnect_timer.timeout.connect(self._connect_if_needed)
        self._performance_timer = QTimer(self)
        self._performance_timer.setInterval(PERFORMANCE_REFRESH_MS)
        self._performance_timer.timeout.connect(self._refresh_performance)

    @property
    def performance_enabled(self) -> bool:
        """Only the managed local Nene export has the known parameter rig."""
        return self.config.model_path is not None and self.config.slot == 1

    @property
    def ready(self) -> bool:
        return not self._closed and self._registered and self.socket.state() == QAbstractSocket.ConnectedState

    def start(self) -> None:
        if self._closed:
            return
        self._reconnect_timer.start()
        self._connect_if_needed()

    def _connect_if_needed(self) -> None:
        if self._closed:
            return
        if self.socket.state() in {
            QAbstractSocket.ConnectedState,
            QAbstractSocket.ConnectingState,
            QAbstractSocket.HostLookupState,
        }:
            return
        self._cancel_model_wait()
        self._registered = False
        self.socket.abort()
        self.socket.open(QUrl(f"ws://127.0.0.1:{self.config.discover_port()}/api"))

    def _send(self, message: int, data=None, *, registration: bool = False) -> bool:
        if self._closed:
            return False
        if self.socket.state() != QAbstractSocket.ConnectedState:
            return False
        if not registration and not self.ready:
            return False
        self._serial += 1
        packet = {"msg": message, "msgId": self._serial}
        if data is not None:
            packet["data"] = data
        return self.socket.sendTextMessage(json.dumps(packet, ensure_ascii=False)) > 0

    def _connected(self) -> None:
        if self._closed:
            return
        self._cancel_model_wait()
        self._performance_timer.stop()
        self._speech_active = False
        self._timeline_motion = None
        self._registered = False
        self._managed_model_valid = False
        self._send(10000, registration=True)

    def _received(self, text: str) -> None:
        if self._closed:
            return
        try:
            packet = json.loads(text)
        except (ValueError, TypeError):
            return
        if not isinstance(packet, dict):
            return
        if packet.get("msg") == 10002:
            # Viewer owns the real model hit areas even after its window/model
            # moves. Relay only this model's documented Tap event; other slots
            # and event kinds must never focus our input window.
            data = packet.get("data")
            if (
                self.ready and isinstance(data, dict)
                and type(data.get("type")) is int and data["type"] == 0
                and type(data.get("id")) is int and data["id"] == self.config.slot
            ):
                self.model_tapped.emit()
            return
        if packet.get("msg") != 10000:
            return
        if self._registered:
            return  # Duplicate ACKs must not replace/reload a model twice.
        self._registered = True
        self._last_warning = ""
        self.status_changed.emit(True, "Live2DViewerEX 已连接")
        if self.config.model_path is not None:
            if self.config.slot != 1:
                self._warn("工程导出模型仅允许加载到宁宁的 slot 1；保留 Viewer 当前模型。")
                return
            try:
                model_path = validate_model3_path(self.config.model_path)
            except (OSError, ValueError) as exc:
                self._warn(f"工程导出模型不可用，保留 Viewer 当前模型：{exc}")
                return
            self.end_drag()
            self._model_loading = True
            if not self._send(13000, {"id": 1, "file": str(model_path)}):
                self._model_loading = False
                self._warn("工程导出模型加载请求未能发送；尚未确认 Viewer 当前显示。")
                return
            self._managed_model_valid = True
            LOGGER.info("工程导出模型加载请求已发送 | slot=1 | model=%s | 尚未确认画面", model_path)
            self.status_changed.emit(True, "Live2DViewerEX 已连接；已发送工程导出模型加载请求，画面待核对")
            generation = self._model_generation
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(MODEL_LOAD_SETTLE_MS)
            timer.timeout.connect(lambda: self._finish_model_wait(generation))
            self._model_load_timer = timer
            timer.start()
            return
        self._restore_managed_position()
        # Legacy models retain their old startup behavior: no face or costume
        # is restored; the next sentence chooses its face motion.

    def _restore_managed_position(self) -> None:
        if self.config.managed_position:
            if self._position is None:
                self._warn("受控位置模式未配置 AIPET_VIEWER_POSITION，模型移动已禁用。")
            elif self.config.slot != 1:
                self._warn("受控位置模式仅允许宁宁的 slot 1，模型移动已禁用。")
            else:
                self._send(13400, {
                    "id": 1,
                    "posX": self._position[0],
                    "posY": self._position[1],
                })

    def _cancel_model_wait(self) -> None:
        self._model_generation += 1
        self._model_loading = False
        timer, self._model_load_timer = self._model_load_timer, None
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def _finish_model_wait(self, generation: int) -> None:
        if generation != self._model_generation or not self._model_loading:
            return
        self._cancel_model_wait()
        if self._closed or not self.ready:
            return
        self._restore_managed_position()
        if self.performance_enabled:
            self._refresh_performance()
        else:
            self.set_emotion(self._desired_emotion or "平静")
        LOGGER.info("模型加载等待结束，已请求恢复位置与表情 | slot=1 | emotion=%s | 画面仍需核对", self._desired_emotion or "平静")

    def _disconnected(self) -> None:
        self._cancel_model_wait()
        self._performance_timer.stop()
        self._speech_active = False  # Never replay old speech after reconnect.
        self._timeline_motion = None
        self._registered = False
        self._managed_model_valid = False
        self.end_drag()
        self.status_changed.emit(False, "Live2DViewerEX 未连接")
        if not self._closed:
            self._warn(
                "Live2DViewerEX 未连接；文字输入仍可使用，表情和同步拖动暂不可用。"
                "请检查 ViewerEX 是否运行及其端口设置。"
            )

    def _warn(self, message: str) -> None:
        if message != self._last_warning:
            self._last_warning = message
            self.warning.emit(message)

    def set_emotion(self, emotion: str) -> bool:
        emotion = emotion if emotion in MOTION_NAMES else "平静"
        self._desired_emotion = emotion
        if self.performance_enabled:
            return self.set_base_emotion(emotion)
        if self._model_loading or self._closed:
            return False  # Deferred, not claimed to have reached the new model.
        path = MOTION_ROOT / f"{MOTION_NAMES[emotion]}.motion3.json"
        if not path.is_file():
            self._warn(f"缺少宁宁脸部动作：{path.name}")
            return False
        sent = self._send(13200, {
            "id": self.config.slot,
            "type": 1,
            "mtn": str(path.resolve()),
        })
        if sent:
            self._emotion = emotion
        return sent

    def set_base_emotion(self, emotion: str, strength: float = .65) -> bool:
        emotion = emotion if emotion in MOTION_NAMES else "平静"
        try:
            strength = float(strength)
        except (TypeError, ValueError):
            strength = .65
        strength = min(1.0, max(0.0, strength)) if math.isfinite(strength) else .65
        changed = (emotion, strength) != (self._base_emotion, self._base_strength)
        self._base_emotion, self._base_strength = emotion, strength
        self._desired_emotion = emotion
        if not self.performance_enabled:
            return self.set_emotion(emotion) if changed else False
        if not changed or self._speech_active or self._paused:
            return False
        return self._refresh_performance()

    def set_activity(self, phase: str, attention=None) -> bool:
        # Speech is owned exclusively by play_speech/finish_speech. Pausing
        # is owned by set_paused, so callers cannot start semantic DND actions.
        if self._paused or phase not in {"idle", "listening", "thinking"}:
            return False
        look = self._attention
        if attention is not None:
            try:
                values = tuple(float(value) for value in attention)
                if len(values) == 2 and all(math.isfinite(value) for value in values):
                    look = tuple(min(1.0, max(-1.0, value)) for value in values)
            except (TypeError, ValueError):
                pass
        changed = (phase, look) != (self._activity, self._attention)
        self._activity, self._attention = phase, look
        if not self.performance_enabled or not changed or self._speech_active or self._paused:
            return False
        return self._refresh_performance()

    def set_paused(self, paused: bool) -> None:
        paused = bool(paused)
        if paused == self._paused:
            return
        self._paused = paused
        self._speech_active = False
        self._timeline_motion = None
        self._activity, self._attention = "idle", (0.0, 0.0)
        self._performance_timer.stop()
        if self.performance_enabled:
            self._refresh_performance()

    def play_speech(self, emotion: str, audio_path: Path | None,
                    duration_seconds: float) -> bool:
        """Request one finite audio-derived clip immediately before QSound.

        This transport has no playback ACK or shared audio clock. It neither
        starts audio nor promises sample-exact synchronization, and never
        queues speech while disconnected or waiting for a model load.
        """
        self._timeline_motion = None
        emotion = emotion if emotion in MOTION_NAMES else "平静"
        if not self.performance_enabled:
            return self.set_emotion(emotion)
        if (self._closed or self._paused or self._model_loading or not self.ready
                or not self._managed_model_valid):
            return False
        self._performance_timer.stop()
        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError):
            duration = 0.0
        valid_duration = math.isfinite(duration) and 0 < duration <= 120
        # Overlong, undecodable or absent audio must leave the mouth closed;
        # none of these presentation failures should block the actual audio.
        duration = duration if valid_duration else PERFORMANCE_CLIP_SECONDS
        sent = self._send_performance(
            emotion=emotion, phase="speaking" if audio_path is not None and valid_duration else "idle",
            duration=duration, audio_path=Path(audio_path) if audio_path is not None and valid_duration else None,
        )
        self._speech_active = sent
        if not sent:
            self._arm_performance_timer()
        return sent

    def prepare_timeline_speech(self, emotion: str, audio_path: Path, duration_seconds: float) -> bool:
        """Prepare locally; actual output progress will schedule short windows."""
        self._timeline_motion = None
        if (not self.performance_enabled or self._closed or self._paused
                or self._model_loading or not self.ready or not self._managed_model_valid):
            return False
        try:
            self._motion_seed += 1
            self._timeline_motion = compose_motion(emotion=emotion, audio_path=audio_path,
                duration=duration_seconds, phase="speaking", attention=self._attention,
                seed=self._motion_seed, strength=self._base_strength)
        except (OSError, ValueError):
            LOGGER.warning("音频时间轴动作准备失败，保留原表演路径")
            return False
        self._timeline_next_position = 0.0
        self._speech_active = True
        self._performance_timer.stop()
        return True

    def sync_speech_position(self, seconds: float, *, force: bool = False) -> bool:
        """Align animation requests to processed audio; Viewer has no clock ACK."""
        if (self._timeline_motion is None or not self._speech_active or self._paused
                or self._closed or self._model_loading or not self.ready):
            return False
        try:
            position = float(seconds)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(position) or position < 0 or (position < self._timeline_next_position and not force):
            return False
        clip = motion_window(self._timeline_motion, position, 2.0)
        sent = self._send_composed_motion(clip, retain_seconds=10.0)
        if sent:
            self._timeline_next_position = position + 1.6
        return sent

    def finish_speech(self) -> None:
        self._timeline_motion = None
        if not self._speech_active:
            return
        self._speech_active = False
        if self.performance_enabled:
            self._refresh_performance()

    def _arm_performance_timer(self) -> None:
        if (self.performance_enabled and not self._closed and self.ready
                and self._managed_model_valid and not self._model_loading
                and not self._paused and not self._speech_active):
            self._performance_timer.start()
        else:
            self._performance_timer.stop()

    def _refresh_performance(self) -> bool:
        if (not self.performance_enabled or self._closed or self._model_loading
                or not self._managed_model_valid or not self.ready or self._speech_active):
            return False
        sent = self._send_performance(
            emotion=self._base_emotion, phase="paused" if self._paused else self._activity,
            duration=PERFORMANCE_CLIP_SECONDS,
        )
        self._arm_performance_timer()
        return sent

    def _send_performance(self, *, emotion: str, phase: str, duration: float,
                          audio_path: Path | None = None) -> bool:
        if self._closed or self._model_loading or not self.ready or not self._managed_model_valid:
            return False
        self._prune_motion_cache()
        if len(self._motion_files) >= MOTION_CACHE_LIMIT:
            self._warn("临时动作缓存繁忙，本次仅保留现有动画；语音播放不受影响。")
            return False
        try:
            self._motion_seed += 1
            arguments = dict(emotion=emotion, duration=duration, phase=phase,
                             attention=(0.0, 0.0) if phase == "paused" else self._attention,
                             seed=self._motion_seed, strength=self._base_strength)
            try:
                motion = compose_motion(**arguments, audio_path=audio_path)
            except Exception:
                if audio_path is None:
                    raise
                LOGGER.warning("口型音频读取失败，使用闭嘴表情动作")
                motion = compose_motion(**{**arguments, "phase": "idle"}, audio_path=None)
            if phase == "paused":
                # No refresh/semantic decisions during DND. The loop contains
                # only silent blinking/breathing produced by the paused phase.
                motion["Meta"]["Loop"] = True
        except Exception:
            LOGGER.exception("本地表演动作生成失败，保留现有模型动画")
            return False
        sent = self._send_composed_motion(motion, retain_seconds=MOTION_CACHE_RETAIN_SECONDS)
        if sent:
            self._emotion = emotion
        return sent

    def _send_composed_motion(self, motion: dict, *, retain_seconds: float) -> bool:
        if self._closed or self._model_loading or not self.ready or not self._managed_model_valid:
            return False
        self._prune_motion_cache()
        if len(self._motion_files) >= MOTION_CACHE_LIMIT:
            self._warn("临时动作缓存繁忙，本次仅保留现有动画；语音播放不受影响。")
            return False
        path = None
        try:
            self._motion_seed += 1
            if self._motion_directory is None:
                directory = get_cache_dir() / "live2d-performance" / uuid.uuid4().hex
                directory.mkdir(parents=True, exist_ok=False)
                self._motion_directory = directory
            path = self._motion_directory / f"{self._motion_seed:08d}.motion3.json"
            path.write_text(json.dumps(motion, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        except Exception:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            LOGGER.exception("本地表演动作生成失败，保留现有模型动画")
            return False
        now = time.monotonic()
        self._motion_files.append((path, now + max(retain_seconds, motion["Meta"]["Duration"] + 5)))
        sent = self._send(13200, {"id": 1, "type": 1, "mtn": str(path.resolve())})
        if sent:
            self._last_motion_file, self._last_motion_sent_at = path, now
        return sent

    def _prune_motion_cache(self) -> None:
        now = time.monotonic()
        retained = []
        for path, expiry in self._motion_files:
            if expiry > now or path == self._last_motion_file:
                retained.append((path, expiry))
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                retained.append((path, expiry))
        self._motion_files = retained

    def _clean_motion_cache(self) -> None:
        # Delete only exact files generated by this instance. ViewerEX reads
        # paths asynchronously; a recently sent final file gets a grace period.
        directory = self._motion_directory
        recent = self._last_motion_file if time.monotonic() - self._last_motion_sent_at < 30 else None
        paths = [path for path, _ in self._motion_files]
        self._motion_files = []

        def clean(paths):
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            if directory is not None:
                try:
                    directory.rmdir()  # Never recursively remove user files.
                except OSError:
                    pass

        clean([path for path in paths if path != recent])
        if recent is not None:
            # This closure deliberately retains paths only, not the QObject.
            QTimer.singleShot(30000, lambda: clean([recent]))

    def begin_drag(self, overlay_position: QPoint) -> bool:
        if self._model_loading:
            self._warn("工程导出模型正在等待加载，暂时无法同步拖动。")
            return False
        if self.config.managed_position and self.config.slot != 1:
            self._warn("受控位置模式仅允许宁宁的 slot 1，模型移动已禁用。")
            return False
        if self._position is None:
            self._warn("拖动尚未校准：请设置 AIPET_VIEWER_POSITION 为当前模型的 ExAPI 坐标。")
            return False
        if not self.ready:
            self._warn("Live2DViewerEX 尚未连接，暂时无法同步拖动。")
            return False
        self._drag_start = QPoint(overlay_position)
        self._drag_position = self._position
        return True

    def drag_to(self, overlay_position: QPoint) -> bool:
        if self._model_loading or self._drag_start is None or self._drag_position is None:
            return False
        delta = overlay_position - self._drag_start
        position = (
            self._drag_position[0] + delta.x(),
            self._drag_position[1] - delta.y(),
        )
        sent = self._send(13400, {
            "id": self.config.slot,
            "posX": position[0],
            "posY": position[1],
        })
        if sent:
            self._position = position
        return sent

    def end_drag(self) -> None:
        self._drag_start = None
        self._drag_position = None

    def stop(self) -> None:
        if self._closed:
            return
        if self.ready:
            self._send(10001)
        self._closed = True
        self._cancel_model_wait()
        self._reconnect_timer.stop()
        self._performance_timer.stop()
        self._speech_active = False
        self.end_drag()
        self._timeline_motion = None
        self._registered = False
        self.socket.close()
        self._clean_motion_cache()
