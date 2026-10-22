from __future__ import annotations

import os
import struct
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtMultimedia import QAudio, QAudioFormat
from PyQt5.QtWidgets import QApplication

from aipet.ui.audio_playback import AudioPlayback, _load_pcm


class Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class Timer:
    def __init__(self):
        self.timeout = Signal()
        self.active = False
        self.interval = None

    def setInterval(self, interval):
        self.interval = interval

    def start(self):
        self.active = True

    def stop(self):
        self.active = False


class Device:
    def __init__(self, *, supported=True, missing=False):
        self.supported = supported
        self.missing = missing
        self.formats = []

    def isNull(self):
        return self.missing

    def isFormatSupported(self, audio_format):
        self.formats.append(audio_format)
        return self.supported


class Output:
    def __init__(self, *, error=QAudio.NoError, start_state=QAudio.ActiveState):
        self.stateChanged = Signal()
        self.current_state = QAudio.StoppedState
        self.current_error = error
        self.start_state = start_state
        self.microseconds = 0
        self.source = None
        self.stops = 0
        self.deleted = False
        self.emit_on_start = True

    def start(self, source):
        self.source = source
        self.current_state = self.start_state
        if self.emit_on_start:
            self.stateChanged.emit(self.current_state)

    def stop(self):
        self.stops += 1
        self.current_state = QAudio.StoppedState
        self.stateChanged.emit(QAudio.StoppedState)

    def state(self):
        return self.current_state

    def error(self):
        return self.current_error

    def processedUSecs(self):
        return self.microseconds

    def deleteLater(self):
        self.deleted = True

    def idle(self, *, error=QAudio.NoError, drain=True):
        if drain:
            self.source.readAll()
        self.current_error = error
        self.current_state = QAudio.IdleState
        self.stateChanged.emit(self.current_state)


def chunk(tag, data):
    return tag + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) % 2 else b"")


def wav_bytes(*, bits=16, channels=1, rate=24000, data=None, codec=1, align=None, byte_rate=None, extra=b""):
    size = channels * (bits // 8)
    if data is None:
        data = b"\x00" * (rate * size)
    fmt = struct.pack("<HHIIHH", codec, channels, rate, rate * size if byte_rate is None else byte_rate,
                      size if align is None else align, bits)
    return riff(chunk(b"fmt ", fmt + extra) + chunk(b"data", data))


def riff(body):
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body


class AudioPlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "speech.wav"
        self.path.write_bytes(wav_bytes())
        self.timer = Timer()
        self.device = Device()
        self.outputs = []
        self.next_output = None
        self.factory_calls = []

        def factory(device, audio_format, parent):
            self.factory_calls.append((device, audio_format, parent))
            output = self.next_output or Output()
            self.next_output = None
            self.outputs.append(output)
            return output

        self.factory = factory
        self.player = AudioPlayback(audio_output_factory=factory, timer=self.timer, audio_device=self.device)
        self.addCleanup(self.player.stop)
        self.events = []
        self.player.started.connect(lambda: self.events.append(("started",)))
        self.player.position_changed.connect(lambda seconds: self.events.append(("position", seconds)))
        self.player.finished.connect(lambda: self.events.append(("finished",)))
        self.player.failed.connect(lambda error: self.events.append(("failed", error)))

    def count(self, name):
        return sum(event[0] == name for event in self.events)

    def test_process_clock_does_not_advance_with_timer_ticks_alone(self):
        self.assertTrue(self.player.play(self.path))
        output = self.outputs[0]
        self.assertEqual(self.timer.interval, 30)
        self.assertTrue(self.player.is_playing)
        self.assertEqual(self.count("started"), 1)
        self.timer.timeout.emit()
        self.timer.timeout.emit()
        self.assertEqual(self.player.position_seconds(), 0)
        output.microseconds = 312_500
        self.timer.timeout.emit()
        self.assertEqual(self.events[-1], ("position", .3125))
        self.assertEqual(self.player.position_seconds(), .3125)

    def test_clock_is_monotonic_bounded_but_duration_does_not_finish(self):
        self.assertTrue(self.player.play(self.path))
        output = self.outputs[0]
        output.microseconds = 400_000
        self.assertEqual(self.player.position_seconds(), .4)
        output.microseconds = 100_000
        self.assertEqual(self.player.position_seconds(), .4)
        output.microseconds = 90_000_000
        for _ in range(5):
            self.timer.timeout.emit()
        self.assertEqual(self.player.position_seconds(), 1)
        self.assertTrue(self.player.is_playing)
        self.assertEqual(self.count("finished"), 0)

    def test_idle_exhausted_source_finishes_once_and_releases_owned_resources(self):
        self.player.play(self.path)
        output = self.outputs[0]
        old_state = self.player._state_callback
        old_tick = self.player._tick_callback
        output.microseconds = 1_000_000
        output.idle()
        old_state(QAudio.IdleState)
        old_tick()
        self.assertEqual(self.count("finished"), 1)
        self.assertEqual(self.count("failed"), 0)
        self.assertEqual(self.events[-2:], [("position", 1.), ("finished",)])
        self.assertFalse(self.player.is_playing)
        self.assertFalse(self.timer.active)
        self.assertTrue(output.deleted)
        self.assertEqual(output.stops, 1)
        self.assertFalse(output.source.isOpen())
        self.assertIsNone(self.player._output)
        self.assertIsNone(self.player._source)
        self.assertIsNone(self.player._bytes)
        self.assertEqual(output.stateChanged.slots, [])
        self.assertEqual(self.timer.timeout.slots, [])

    def test_eof_underrun_is_normal_but_early_underrun_is_failure(self):
        self.player.play(self.path)
        self.outputs[-1].idle(error=QAudio.UnderrunError)
        self.assertEqual(self.count("finished"), 1)
        self.assertTrue(self.player.play(self.path))
        self.outputs[-1].idle(error=QAudio.UnderrunError, drain=False)
        self.assertEqual(self.count("finished"), 1)
        self.assertEqual(self.count("failed"), 1)
        self.assertFalse(self.timer.active)

    def test_idle_with_fatal_error_is_failure_even_at_eof(self):
        self.player.play(self.path)
        self.outputs[-1].idle(error=QAudio.FatalError)
        self.assertEqual(self.count("failed"), 1)
        self.assertEqual(self.count("finished"), 0)

    def test_stop_preserves_processed_position_and_does_not_emit_finished(self):
        self.player.play(self.path)
        output = self.outputs[0]
        output.microseconds = 250_000
        old_tick = self.player._tick_callback
        old_state = self.player._state_callback
        self.player.stop()
        self.player.stop()
        old_tick()
        old_state(QAudio.IdleState)
        self.assertEqual(self.player.position_seconds(), .25)
        self.assertFalse(self.timer.active)
        self.assertFalse(self.player.is_playing)
        self.assertEqual(self.count("finished"), 0)
        self.assertEqual(self.count("failed"), 0)
        self.assertEqual(output.stops, 1)

    def test_replacement_cannot_be_advanced_or_ended_by_old_callbacks(self):
        self.player.play(self.path)
        old_output = self.outputs[-1]
        old_tick = self.player._tick_callback
        old_state = self.player._state_callback
        old_output.microseconds = 900_000
        self.assertTrue(self.player.play(self.path))
        new_output = self.outputs[-1]
        new_output.microseconds = 100_000
        old_state(QAudio.StoppedState)
        old_state(QAudio.IdleState)
        old_tick()
        self.assertTrue(self.player.is_playing)
        self.assertEqual(self.count("failed"), 0)
        self.assertEqual(self.count("finished"), 0)
        self.assertEqual(self.player.position_seconds(), .1)
        self.assertTrue(old_output.deleted)
        self.assertEqual(len(self.timer.timeout.slots), 1)

    def test_stopped_and_system_suspension_are_failures_not_fake_completion(self):
        for state in (QAudio.StoppedState, QAudio.SuspendedState, QAudio.InterruptedState):
            with self.subTest(state=state):
                self.assertTrue(self.player.play(self.path))
                output = self.outputs[-1]
                output.source.readAll()
                output.current_state = state
                output.stateChanged.emit(state)
                self.assertFalse(self.player.is_playing)
                self.assertFalse(self.timer.active)
        self.assertEqual(self.count("failed"), 3)
        self.assertEqual(self.count("finished"), 0)

    def test_synchronous_open_error_returns_false_and_emits_failure_once(self):
        self.next_output = Output(error=QAudio.OpenError, start_state=QAudio.StoppedState)
        self.assertFalse(self.player.play(self.path))
        self.assertEqual(self.count("failed"), 1)
        self.assertEqual(self.count("started"), 0)
        self.assertFalse(self.timer.active)
        self.assertTrue(self.outputs[-1].deleted)

    def test_poll_checks_state_if_backend_omits_a_signal(self):
        output = self.next_output = Output()
        output.emit_on_start = False
        self.assertTrue(self.player.play(self.path))
        self.assertEqual(self.count("started"), 1)
        output.current_error = QAudio.IOError
        output.current_state = QAudio.StoppedState
        self.timer.timeout.emit()
        self.assertEqual(self.count("failed"), 1)
        self.assertFalse(self.timer.active)

    def test_unsupported_or_missing_device_fails_without_factory(self):
        for missing, supported, message in ((True, True, "No audio output"), (False, False, "exact PCM")):
            with self.subTest(missing=missing):
                self.device.missing = missing
                self.device.supported = supported
                self.assertFalse(self.player.play(self.path))
                self.assertIn(message, self.events[-1][1])
        self.assertEqual(self.factory_calls, [])
        self.assertFalse(self.timer.active)

    def test_pcm_formats_pass_exact_signedness_endian_and_rate(self):
        for channels in (1, 2):
            for bits in (8, 16, 24, 32):
                with self.subTest(channels=channels, bits=bits):
                    self.path.write_bytes(wav_bytes(channels=channels, bits=bits, rate=44100))
                    self.assertTrue(self.player.play(self.path))
                    _, fmt, parent = self.factory_calls[-1]
                    self.assertIs(parent, self.player)
                    self.assertEqual(fmt.channelCount(), channels)
                    self.assertEqual(fmt.sampleSize(), bits)
                    self.assertEqual(fmt.sampleRate(), 44100)
                    self.assertEqual(fmt.byteOrder(), QAudioFormat.LittleEndian)
                    self.assertEqual(fmt.sampleType(), QAudioFormat.UnSignedInt if bits == 8 else QAudioFormat.SignedInt)
                    self.assertEqual(fmt.codec(), "audio/pcm")

    def test_start_slot_can_stop_without_restarting_timer(self):
        self.player.started.connect(self.player.stop)
        self.assertTrue(self.player.play(self.path))
        self.assertFalse(self.player.is_playing)
        self.assertFalse(self.timer.active)
        self.assertEqual(self.count("finished"), 0)

    def test_final_position_slot_can_replace_without_old_finished(self):
        restarted = []

        def restart(seconds):
            if seconds == 1 and not restarted:
                restarted.append(True)
                self.player.play(self.path)

        self.player.position_changed.connect(restart)
        self.player.play(self.path)
        self.outputs[0].microseconds = 1_000_000
        self.outputs[0].idle()
        self.assertEqual(len(self.outputs), 2)
        self.assertTrue(self.player.is_playing)
        self.assertEqual(self.count("finished"), 0)

    def test_failed_slot_can_retry_without_changing_original_false_return(self):
        retried = []

        def retry(error):
            if not retried:
                retried.append(True)
                self.player.play(self.path)

        self.player.failed.connect(retry)
        self.next_output = Output(error=QAudio.OpenError, start_state=QAudio.StoppedState)
        self.assertFalse(self.player.play(self.path))
        self.assertTrue(self.player.is_playing)
        self.assertEqual(len(self.outputs), 2)

    def test_missing_file_fails_without_exposing_path(self):
        self.assertFalse(self.player.play(Path(self.directory.name) / "private-name.wav"))
        self.assertNotIn(self.directory.name, self.events[-1][1])
        self.assertNotIn("private-name", self.events[-1][1])

    def test_invalid_wav_rejected_before_output_is_created(self):
        valid = wav_bytes()
        fmt = chunk(b"fmt ", struct.pack("<HHIIHH", 1, 1, 24000, 48000, 2, 16))
        malformed = {
            "not_wave": b"unrelated data",
            "big_endian": b"RIFX" + valid[4:],
            "short_header": b"RIFF",
            "short_riff": valid[:-1],
            "extra_trailing": valid + b"x",
            "missing_data": riff(fmt),
            "missing_fmt": riff(chunk(b"data", b"xx")),
            "empty_data": wav_bytes(data=b""),
            "float": wav_bytes(codec=3),
            "extensible": wav_bytes(codec=0xFFFE),
            "multichannel": wav_bytes(channels=3),
            "bad_sample_size": wav_bytes(bits=12),
            "low_rate": wav_bytes(rate=7999),
            "high_rate": wav_bytes(rate=192001),
            "bad_align": wav_bytes(align=1),
            "bad_byte_rate": wav_bytes(byte_rate=10),
            "partial_frame": wav_bytes(data=b"x"),
            "duplicate_data": riff(fmt + chunk(b"data", b"xx") + chunk(b"data", b"xx")),
            "duplicate_fmt": riff(fmt + fmt + chunk(b"data", b"xx")),
            "short_chunk_header": riff(fmt + b"xx"),
            "short_chunk": riff(fmt + b"data" + struct.pack("<I", 90) + b"xx"),
            "invalid_fmt_extension": wav_bytes(extra=b"\x02\x00"),
            "oversized_fmt_extension": wav_bytes(extra=b"\x00\x00\x00\x00"),
            "missing_padding": riff(fmt + b"data" + struct.pack("<I", 1) + b"x"),
        }
        for name, data in malformed.items():
            with self.subTest(name=name):
                self.path.write_bytes(data)
                self.assertFalse(self.player.play(self.path))
                self.assertEqual(self.events[-1][0], "failed")
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.count("finished"), 0)

    def test_pcm_18_byte_fmt_and_metadata_chunks_are_supported(self):
        data = wav_bytes(extra=b"\x00\x00")
        raw = riff(chunk(b"JUNK", b"abc") + data[12:] + chunk(b"LIST", b"metadata"))
        self.path.write_bytes(raw)
        self.assertTrue(self.player.play(self.path))
        self.assertEqual(self.player._source.size(), 48000)

    def test_size_limit_is_checked_before_start(self):
        player = AudioPlayback(audio_output_factory=self.factory, timer=Timer(), audio_device=self.device, max_audio_bytes=100)
        self.addCleanup(player.stop)
        failures = []
        player.failed.connect(failures.append)
        self.assertFalse(player.play(self.path))
        self.assertIn("size limit", failures[0])
        self.assertEqual(self.factory_calls, [])

    def test_constructor_rejects_unbounded_allocations(self):
        for limit in (0, 43, 64 * 1024 * 1024 + 1, None, 1.5):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    AudioPlayback(max_audio_bytes=limit)


if __name__ == "__main__":
    unittest.main()
