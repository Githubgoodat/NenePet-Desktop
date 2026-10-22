"""Finite PCM speech playback with a Qt audio-processing clock.

``position_changed`` and ``position_seconds`` use QAudioOutput.processedUSecs(),
not elapsed wall time. They report data processed by the platform audio backend;
device/driver/output buffering can still delay sound reaching a listener. Neither
``started`` nor ``finished`` proves that a person heard the audio. Completion is
the backend's IdleState *and* an exhausted source, never a duration timer.

Only little-endian RIFF/WAVE PCM 8/16/24/32-bit mono/stereo at 8-192 kHz is
accepted. Files are bounded to 64 MiB by default. The exact format must be
supported by the chosen output device: changing a QAudioFormat alone would not
resample its PCM bytes. Callers may choose an explicitly disclosed fallback.

Qt 5.15 upstream API documentation:
https://raw.githubusercontent.com/qt/qtmultimedia/5.15/src/multimedia/audio/qaudiooutput.cpp
https://raw.githubusercontent.com/qt/qtmultimedia/5.15/src/multimedia/audio/qaudio.cpp

All methods must run on this QObject's thread. ``audio_output_factory`` accepts
(audio_device, audio_format, parent). ``timer`` and ``audio_device`` can be
injected for deterministic tests without accessing an output device.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PyQt5.QtCore import QByteArray, QBuffer, QIODevice, QObject, QTimer, pyqtSignal
from PyQt5.QtMultimedia import QAudio, QAudioDeviceInfo, QAudioFormat, QAudioOutput


MAX_AUDIO_BYTES = 64 * 1024 * 1024
POSITION_INTERVAL_MS = 30


@dataclass(frozen=True)
class _PCM:
    data: bytes
    sample_rate: int
    channels: int
    bits: int

    @property
    def duration(self) -> float:
        return len(self.data) / (self.sample_rate * self.channels * (self.bits // 8))

    def qt_format(self) -> QAudioFormat:
        result = QAudioFormat()
        result.setCodec("audio/pcm")
        result.setSampleRate(self.sample_rate)
        result.setChannelCount(self.channels)
        result.setSampleSize(self.bits)
        result.setByteOrder(QAudioFormat.LittleEndian)
        result.setSampleType(QAudioFormat.UnSignedInt if self.bits == 8 else QAudioFormat.SignedInt)
        return result


def _load_pcm(path: Path, max_bytes: int) -> _PCM:
    """Validate RIFF/chunk bounds and PCM layout before allocating Qt buffers."""
    if path.stat().st_size > max_bytes:
        raise ValueError("WAV exceeds the audio size limit")
    # Also bound the read if a file is replaced or grows after stat().
    with path.open("rb") as source:
        raw = source.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("WAV exceeds the audio size limit")
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("Audio is not a little-endian RIFF/WAVE file")
    if struct.unpack_from("<I", raw, 4)[0] + 8 != len(raw):
        raise ValueError("WAV RIFF size does not match the file")

    offset = 12
    fmt = None
    pcm = None
    while offset < len(raw):
        if len(raw) - offset < 8:
            raise ValueError("WAV has a truncated chunk header")
        tag, size = struct.unpack_from("<4sI", raw, offset)
        start = offset + 8
        end = start + size
        padded_end = end + (size & 1)
        if padded_end > len(raw):
            raise ValueError("WAV has a truncated chunk or missing padding")
        if tag == b"fmt ":
            if fmt is not None or size not in (16, 18):
                raise ValueError("WAV needs one standard PCM format chunk")
            if size == 18 and struct.unpack_from("<H", raw, start + 16)[0] != 0:
                raise ValueError("WAV PCM format extensions are not supported")
            codec, channels, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", raw, start)
            if codec != 1:
                raise ValueError("WAV must use integer PCM encoding")
            if channels not in (1, 2) or bits not in (8, 16, 24, 32):
                raise ValueError("WAV must be mono/stereo PCM 8/16/24/32-bit")
            if not 8_000 <= rate <= 192_000:
                raise ValueError("WAV sample rate must be between 8000 and 192000 Hz")
            expected_align = channels * (bits // 8)
            if block_align != expected_align or byte_rate != rate * expected_align:
                raise ValueError("WAV block alignment or byte rate is inconsistent")
            fmt = (rate, channels, bits, block_align)
        elif tag == b"data":
            if fmt is None or pcm is not None or size == 0:
                raise ValueError("WAV needs one nonempty data chunk after its format")
            if size % fmt[3]:
                raise ValueError("WAV data ends inside a PCM frame")
            pcm = raw[start:end]
        offset = padded_end
    if fmt is None or pcm is None:
        raise ValueError("WAV is missing its format or audio data")
    return _PCM(pcm, fmt[0], fmt[1], fmt[2])


class AudioPlayback(QObject):
    """Own one finite speech stream; stop/restart invalidates queued callbacks.

    ``play`` returns False and emits ``failed`` for synchronous format/device
    failures. Later device failures emit ``failed`` once. ``stop`` is silent and
    never emits ``finished``. ``started`` means ActiveState was observed.
    """

    started = pyqtSignal()
    position_changed = pyqtSignal(float)
    finished = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        audio_output_factory: Callable | None = None,
        timer=None,
        audio_device=None,
        max_audio_bytes: int = MAX_AUDIO_BYTES,
    ) -> None:
        super().__init__(parent)
        if not isinstance(max_audio_bytes, int) or not 44 <= max_audio_bytes <= MAX_AUDIO_BYTES:
            raise ValueError("max_audio_bytes must be between 44 and 64 MiB")
        self._factory = audio_output_factory or QAudioOutput
        self._device_override = audio_device
        self._max_bytes = max_audio_bytes
        self._timer = timer if timer is not None else QTimer(self)
        self._timer.setInterval(POSITION_INTERVAL_MS)
        self._generation = 0
        self._output = None
        self._source = None
        self._bytes = None
        self._state_callback = None
        self._tick_callback = None
        self._playing = False
        self._started = False
        self._position = 0.0
        self._duration = 0.0
        self._attempt_failed = None

    @property
    def is_playing(self) -> bool:
        return self._playing

    def play(self, path: Path) -> bool:
        self.stop()
        generation = self._generation
        attempt_failed = [False]
        self._attempt_failed = attempt_failed
        self._position = 0.0
        self._duration = 0.0
        self._started = False
        try:
            pcm = _load_pcm(Path(path), self._max_bytes)
            audio_format = pcm.qt_format()
            device = self._device_override
            if device is None:
                device = QAudioDeviceInfo.defaultOutputDevice()
            if device.isNull():
                raise ValueError("No audio output device is available")
            if not device.isFormatSupported(audio_format):
                raise ValueError("Audio output device does not support this exact PCM format")
            self._duration = pcm.duration
            self._bytes = QByteArray(pcm.data)
            self._source = QBuffer(self)
            # setData uses the buffer's own QByteArray rather than retaining a
            # pointer to an external array across deferred QObject deletion.
            self._source.setData(self._bytes)
            if not self._source.open(QIODevice.ReadOnly):
                raise ValueError("Could not open the in-memory audio source")
            self._output = self._factory(device, audio_format, self)
            self._state_callback = lambda state: self._on_state(generation, state)
            self._output.stateChanged.connect(self._state_callback)
            self._tick_callback = lambda: self._on_tick(generation)
            self._timer.timeout.connect(self._tick_callback)
            self._playing = True
            self._output.start(self._source)
            # Some platform backends signal state synchronously; some only
            # expose it via state() initially. Both use the same guarded path.
            if self._is_current(generation):
                self._on_state(generation, self._output.state())
            if self._is_current(generation):
                self._timer.start()
        except ValueError as exc:
            self._fail(generation, str(exc))
        except (OSError, RuntimeError, TypeError, AttributeError, OverflowError):
            self._fail(generation, "Audio output could not start; the WAV file or output device is unavailable")
        return not attempt_failed[0]

    def stop(self) -> None:
        """Immediately detach this generation and release its audio resources."""
        if self._playing:
            self._read_position()
        self._generation += 1
        self._playing = False
        self._release()

    def position_seconds(self) -> float:
        if self._playing:
            self._read_position()
        return self._position

    def _is_current(self, generation: int) -> bool:
        return self._playing and self._generation == generation

    def _read_position(self) -> float:
        if self._output is not None:
            try:
                seconds = max(0, int(self._output.processedUSecs())) / 1_000_000
                self._position = max(self._position, min(self._duration, seconds))
            except (RuntimeError, TypeError, ValueError, AttributeError, OverflowError):
                pass
        return self._position

    def _on_tick(self, generation: int) -> None:
        if not self._is_current(generation):
            return
        self.position_changed.emit(self._read_position())
        # A slot may synchronously stop/restart playback.
        if self._is_current(generation):
            try:
                self._on_state(generation, self._output.state())
            except (RuntimeError, TypeError, AttributeError):
                self._fail(generation, "Audio output device became unavailable")

    def _on_state(self, generation: int, state) -> None:
        if not self._is_current(generation):
            return
        try:
            error = self._output.error()
            if state == QAudio.ActiveState:
                if error not in (QAudio.NoError, QAudio.UnderrunError):
                    self._fail(generation, "Audio output device reported a playback error")
                elif not self._started:
                    self._started = True
                    self.started.emit()
            elif state == QAudio.IdleState:
                at_end = self._source is not None and self._source.atEnd()
                if at_end and error in (QAudio.NoError, QAudio.UnderrunError):
                    self._complete(generation)
                else:
                    self._fail(generation, "Audio output stopped receiving data before the source was exhausted")
            elif state == QAudio.StoppedState:
                self._fail(generation, "Audio output device stopped before normal completion")
            elif state in (QAudio.SuspendedState, QAudio.InterruptedState):
                self._fail(generation, "Audio output was suspended or interrupted by the system")
        except (RuntimeError, TypeError, AttributeError):
            self._fail(generation, "Audio output device became unavailable")

    def _complete(self, generation: int) -> None:
        if not self._is_current(generation):
            return
        position = self._read_position()
        self._playing = False
        self._generation += 1
        terminal_generation = self._generation
        self._release()
        self.position_changed.emit(position)
        if self._generation == terminal_generation:
            self.finished.emit()

    def _fail(self, generation: int, message: str) -> None:
        if self._generation != generation:
            return
        if self._attempt_failed is not None:
            self._attempt_failed[0] = True
        self._playing = False
        self._generation += 1
        self._release()
        self.failed.emit(message)

    def _release(self) -> None:
        self._timer.stop()
        if self._tick_callback is not None:
            try:
                self._timer.timeout.disconnect(self._tick_callback)
            except (TypeError, RuntimeError):
                pass
            self._tick_callback = None
        output, self._output = self._output, None
        source, self._source = self._source, None
        if output is not None:
            if self._state_callback is not None:
                try:
                    output.stateChanged.disconnect(self._state_callback)
                except (TypeError, RuntimeError):
                    pass
            try:
                output.stop()
            except RuntimeError:
                pass
            try:
                output.deleteLater()
            except RuntimeError:
                pass
        self._state_callback = None
        if source is not None:
            try:
                source.close()
                source.deleteLater()
            except RuntimeError:
                pass
        self._bytes = None
