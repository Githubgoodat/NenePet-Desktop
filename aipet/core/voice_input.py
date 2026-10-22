"""Platform-neutral hold-to-talk recording and transcription."""

from __future__ import annotations

import threading
import wave
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

import numpy as np
from aipet.core.audio_devices import (
    audio_backend_access,
    resolve_audio_input_device,
    set_audio_capture_active,
)
from aipet.core.config import get_cache_dir
from aipet.core.runtime_logging import get_logger
from aipet.core.stt import transcribe_full


logger = get_logger("voice")


class AudioRecorder:
    def __init__(
        self,
        samplerate: int = 16000,
        channels: int = 1,
        input_device: str = "",
    ):
        self.samplerate = samplerate
        self.channels = channels
        self.input_device = input_device
        self._stream: Optional[object] = None
        self._stream_active = False
        self._frames = []
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        del frames, time_info
        if status:
            logger.warning("录音设备状态：%s", status)
        with self._lock:
            self._frames.append(indata.copy())

    def start(self) -> None:
        import sounddevice as sd

        with self._lock:
            self._frames = []
        with audio_backend_access():
            device_index = resolve_audio_input_device(self.input_device)
            selected = sd.query_devices(device_index, kind="input")
            stream = sd.InputStream(
                device=device_index,
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="int16",
                callback=self._callback,
            )
            try:
                stream.start()
            except Exception:
                stream.close()
                raise
            self._stream = stream
            self._stream_active = True
            set_audio_capture_active(True)
        logger.info("录音开始 | 输入设备=%s", selected["name"])

    def stop_and_save(self, wav_path: str) -> Optional[str]:
        if self._stream is None:
            return None
        self.close()
        with self._lock:
            if not self._frames:
                logger.warning("没有录到有效音频")
                return None
            data = np.concatenate(self._frames, axis=0)
        try:
            Path(wav_path).parent.mkdir(parents=True, exist_ok=True)
            with wave.open(wav_path, "wb") as output:
                output.setnchannels(self.channels)
                output.setsampwidth(2)
                output.setframerate(self.samplerate)
                output.writeframes(data.tobytes())
            logger.info("录音已保存到临时文件")
            return wav_path
        except Exception:
            logger.exception("保存 WAV 失败")
            return None

    def close(self) -> None:
        stream = self._stream
        if stream is None:
            return
        with audio_backend_access():
            try:
                try:
                    stream.stop()
                finally:
                    stream.close()
            finally:
                self._stream = None
                if self._stream_active:
                    self._stream_active = False
                    set_audio_capture_active(False)


class HoldToTalkSession:
    def __init__(
        self,
        on_text_ready: Callable[[str], None],
        hold_seconds: float = 2.0,
        on_record_start: Optional[Callable[[], None]] = None,
        on_record_end: Optional[Callable[[], None]] = None,
        model_name: str = "large-v3",
        model_directory: str = "",
        device: str = "auto",
        input_device: str = "",
        language: str | None = "zh",
        on_error: Optional[Callable[[str], None]] = None,
        vocabulary_profile: str = "murasame",
    ):
        self.on_text_ready = on_text_ready
        self.hold_seconds = hold_seconds
        self.on_record_start = on_record_start
        self.on_record_end = on_record_end
        self.model_name = model_name
        self.model_directory = model_directory
        self.device = device
        self.input_device = input_device
        self.language = language
        self.on_error = on_error
        self.vocabulary_profile = vocabulary_profile
        self._state_lock = threading.RLock()
        self._session_generation = 0
        self._stopped = False
        self._recognition_thread: threading.Thread | None = None
        self._starting_record = threading.Event()
        self._recognition_pending = threading.Event()
        self._pressed = False
        self._recording = False
        self._recognizing = False
        self._hold_timer: Optional[threading.Timer] = None
        self._recorder = AudioRecorder(input_device=input_device)

    def press(self) -> None:
        with self._state_lock:
            if self._stopped or self.is_busy() or self._recognizing or self._pressed:
                return
            self._pressed = True
            self._hold_timer = threading.Timer(self.hold_seconds, self._maybe_start_record)
            self._hold_timer.daemon = True
            self._hold_timer.start()

    def release(self) -> None:
        with self._state_lock:
            self._pressed = False
            if self._hold_timer is not None:
                self._hold_timer.cancel()
                self._hold_timer = None
            was_recording = self._recording
            self._recording = False
        if was_recording:
            self._handle_record_done()

    def stop_session(self) -> None:
        with self._state_lock:
            self._stopped = True
            self._session_generation += 1
            if self._hold_timer is not None:
                self._hold_timer.cancel()
                self._hold_timer = None
            self._pressed = False
            self._recording = False
            self._recognizing = False
        self._recorder.close()

    def start_session(self) -> None:
        """Permit a stopped trigger to be explicitly started again."""
        with self._state_lock:
            self._stopped = False

    def is_busy(self) -> bool:
        """No model or audio I/O: used to drain background work before exit."""
        with self._state_lock:
            thread = self._recognition_thread
            return (self._starting_record.is_set() or self._recognition_pending.is_set()
                    or bool(thread and thread.is_alive()))

    def _current_session(self, generation: int) -> bool:
        with self._state_lock:
            return not self._stopped and generation == self._session_generation

    def _notify(self, generation: int, callback, *args) -> None:
        # Serialize the final check and callback with stop_session, so no
        # queued Python result can be delivered after stop_session returns.
        with self._state_lock:
            if self._current_session(generation) and callback:
                callback(*args)

    def _maybe_start_record(self) -> None:
        with self._state_lock:
            generation = self._session_generation
            if self._stopped or not self._pressed:
                return
            self._starting_record.set()
            self._recording = True
        try:
            self._recorder.start()
            if not self._current_session(generation) or not self._pressed:
                self._recorder.close()
                self._recording = False
                return
            if self.on_record_start:
                try:
                    self._notify(generation, self.on_record_start)
                except Exception:
                    logger.exception("录音开始回调失败")
        except Exception as exc:
            self._recording = False
            logger.exception("启动录音失败")
            self._notify(generation, self.on_error, str(exc))
        finally:
            self._starting_record.clear()

    def _handle_record_done(self) -> None:
        with self._state_lock:
            if self._stopped:
                return
            generation = self._session_generation
            self._recognizing = True
            self._recognition_pending.set()
        wav_path = str(
            get_cache_dir() / "recordings" / f"{uuid4().hex}.wav"
        )
        try:
            saved = self._recorder.stop_and_save(wav_path)
        except Exception as exc:
            self._recognizing = False
            self._recognition_pending.clear()
            self._notify(generation, self.on_error, str(exc))
            return
        if self._current_session(generation) and self.on_record_end:
            try:
                self._notify(generation, self.on_record_end)
            except Exception:
                logger.exception("录音结束回调失败")
        if not saved:
            self._recognizing = False
            self._recognition_pending.clear()
            self._notify(generation, self.on_error, "没有录到有效音频，请检查麦克风后重试。")
            return

        def transcribe() -> None:
            try:
                if not self._current_session(generation):
                    return
                text = (
                    transcribe_full(
                        saved,
                        model_size=self.model_name,
                        model_directory=self.model_directory,
                        device=self.device,
                        language=self.language,
                        vocabulary_profile=self.vocabulary_profile,
                    )
                    or ""
                ).strip()
                if not self._current_session(generation):
                    return
                if not text:
                    logger.warning("语音识别结果为空")
                    self._notify(generation, self.on_error, "没有识别到有效语音，请重试。")
                    return
                logger.info("语音识别完成（内容不写入日志）")
                try:
                    self._notify(generation, self.on_text_ready, text)
                except Exception:
                    logger.exception("语音识别回调处理失败")
            except Exception as exc:
                logger.exception("语音识别失败")
                self._notify(generation, self.on_error, str(exc))
            finally:
                if generation == self._session_generation:
                    self._recognizing = False
                try:
                    Path(saved).unlink(missing_ok=True)
                    logger.info("语音临时文件已删除")
                except Exception as exc:
                    logger.warning("删除语音临时文件失败：%s", exc)
                self._recognition_pending.clear()

        thread = threading.Thread(target=transcribe, daemon=True)
        self._recognition_thread = thread
        try:
            thread.start()
        except Exception as exc:
            self._recognizing = False
            self._recognition_pending.clear()
            Path(saved).unlink(missing_ok=True)
            logger.exception("启动语音识别线程失败")
            self._notify(generation, self.on_error, str(exc))


__all__ = ["AudioRecorder", "HoldToTalkSession"]

