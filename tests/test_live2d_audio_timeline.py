"""Audio-clock motion windows: local numerical/transport checks, no Viewer UI."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtNetwork import QAbstractSocket
from PyQt5.QtWidgets import QApplication

from aipet.core.live2d_performance import PARAMETER_RANGES, compose_motion, motion_window
from aipet.ui.live2d_renderer import MOTION_CACHE_LIMIT, ViewerEXConfig, ViewerEXRenderer


def points(curve):
    segments = curve["Segments"]
    return list(zip([segments[0], *segments[3::3]], [segments[1], *segments[4::3]]))


def curve_by_id(motion, parameter):
    return next(curve for curve in motion["Curves"] if curve["Id"] == parameter)


def sample(curve, second):
    entries = points(curve)
    if second <= entries[0][0]:
        return entries[0][1]
    for (left_t, left_v), (right_t, right_v) in zip(entries, entries[1:]):
        if second <= right_t:
            return left_v + (right_v - left_v) * (second - left_t) / (right_t - left_t)
    return entries[-1][1]


class FakeSocket(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    textMessageReceived = pyqtSignal(str)
    error = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.connection_state = QAbstractSocket.ConnectedState
        self.packets = []
        self.reject = False

    def state(self):
        return self.connection_state

    def sendTextMessage(self, text):
        if self.reject:
            return 0
        self.packets.append(json.loads(text))
        return len(text)

    def close(self):
        self.connection_state = QAbstractSocket.UnconnectedState
        self.disconnected.emit()


class MotionWindowTests(unittest.TestCase):
    def linear_motion(self):
        motion = compose_motion(duration=4, seed=2)
        motion["Curves"] = [{"Target": "Parameter", "Id": "ParamAngleX",
                             "Segments": [0., 0., 0, 1., 10., 0, 2., 0., 0, 3., -10., 0, 4., 0.]}]
        return motion

    def test_rebases_all_parameter_times_and_interpolates_cut_edges(self):
        motion = self.linear_motion()
        window = motion_window(motion, .5, 2)
        self.assertEqual(window["Meta"]["Duration"], 2)
        self.assertEqual(points(window["Curves"][0]), [(0., 5.), (.5, 10.), (1.5, 0.), (2., -5.)])
        self.assertFalse(window["Meta"]["Loop"])
        self.assertEqual(window["Meta"]["CurveCount"], 1)
        self.assertEqual(window["Meta"]["TotalSegmentCount"], 3)
        self.assertEqual(window["Meta"]["TotalPointCount"], 4)

    def test_source_is_unchanged_and_generated_curve_samples_match(self):
        motion = compose_motion(duration=6, emotion="生气", phase="thinking", attention=(.2, -.1), seed=4)
        original = copy.deepcopy(motion)
        window = motion_window(motion, 1.25, 2.3)
        self.assertEqual(motion, original)
        for curve in window["Curves"]:
            source = curve_by_id(motion, curve["Id"])
            for time, value in points(curve):
                self.assertGreaterEqual(time, 0)
                self.assertLessEqual(time, window["Meta"]["Duration"])
                self.assertAlmostEqual(value, sample(source, time + 1.25), places=5)
            self.assertEqual(points(curve)[0][0], 0)
            self.assertAlmostEqual(points(curve)[-1][0], 2.3)

    def test_exact_keyframe_cut_has_no_duplicate_zero_or_end_points(self):
        window = motion_window(self.linear_motion(), 1, 2)
        self.assertEqual(points(window["Curves"][0]), [(0., 10.), (1., 0.), (2., -10.)])

    def test_near_end_window_is_bounded_to_motion_duration(self):
        window = motion_window(self.linear_motion(), 3.5, 2)
        self.assertEqual(window["Meta"]["Duration"], .5)
        self.assertEqual(points(window["Curves"][0]), [(0., -5.), (.5, 0.)])

    def test_beyond_end_holds_terminal_pose_instead_of_replaying_last_frames(self):
        motion = self.linear_motion()
        for position in (4., 4.1, 1000.):
            with self.subTest(position=position):
                window = motion_window(motion, position, 2)
                self.assertGreaterEqual(window["Meta"]["Duration"], .099999)
                self.assertTrue(all(value == 0 for _, value in points(window["Curves"][0])))

    def test_sound_events_and_userdata_are_not_carried_into_windows(self):
        motion = self.linear_motion()
        motion["Sound"] = "untrusted-native.wav"
        motion["UserData"] = [{"Time": 1, "Value": "Tap"}]
        motion["Meta"].update(UserDataCount=1, TotalUserDataSize=3)
        window = motion_window(motion, 0, 2)
        self.assertNotIn("Sound", window)
        self.assertNotIn("UserData", window)
        self.assertEqual(window["Meta"]["UserDataCount"], 0)
        self.assertEqual(window["Meta"]["TotalUserDataSize"], 0)

    def test_motion_window_rejects_out_of_scope_clothing_and_non_linear_curves(self):
        for kind in ("clothing", "model", "bezier", "short", "bad-length"):
            motion = self.linear_motion()
            curve = motion["Curves"][0]
            if kind == "clothing":
                curve["Id"] = "ParamClothing"
            elif kind == "model":
                curve["Target"] = "Model"
            elif kind == "bezier":
                curve["Segments"][2] = 1
            elif kind == "short":
                curve["Segments"] = [0., 1.]
            else:
                curve["Segments"].append(5.)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                motion_window(motion, 0, 1)

    def test_invalid_window_arguments_rejected(self):
        for start, duration in ((-1, 1), (math.nan, 1), (math.inf, 1), (0, 0), (0, .09), (0, math.nan), (0, "bad")):
            with self.subTest(start=start, duration=duration), self.assertRaises(ValueError):
                motion_window(self.linear_motion(), start, duration)


class Live2DAudioTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix=".test-audio-timeline-", dir=Path(__file__).resolve().parents[1]))
        self.directory = Path(directory)
        self.enterContext(patch.dict(os.environ, {"AIPET_CACHE_DIR": str(self.directory / "cache")}))
        self.model = self.directory / "nene.model3.json"
        self.model.write_text(json.dumps({"Version": 3, "Parameters": {"ParamClothing": 1, "ParamOutfit": 2}}), encoding="utf-8")
        self.model_before = self.model.read_bytes()
        self.audio = self.directory / "speech.wav"
        self.write_speech(seconds=4)
        self.audio_hash = hashlib.sha256(self.audio.read_bytes()).digest()
        self.socket = FakeSocket()
        self.renderer = ViewerEXRenderer(ViewerEXConfig(self.directory / "port", model_path=self.model), socket=self.socket)
        self.renderer._registered = True
        self.renderer._managed_model_valid = True
        self.addCleanup(self.clean_renderer)

    def clean_renderer(self):
        # No asynchronous cleanup after TemporaryDirectory removes our fixture.
        self.renderer._last_motion_sent_at = -1000
        self.renderer.stop()
        self.renderer.deleteLater()

    def write_speech(self, seconds):
        rate = 8000
        with wave.open(str(self.audio), "wb") as destination:
            destination.setnchannels(1)
            destination.setsampwidth(2)
            destination.setframerate(rate)
            destination.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(index * math.tau * 220 / rate))) for index in range(round(seconds * rate))))

    def prepare(self, *, duration=4.5):
        return self.renderer.prepare_timeline_speech("高兴", self.audio, duration)

    def motion_packets(self):
        return [packet for packet in self.socket.packets if packet["msg"] == 13200]

    def last_motion(self):
        packet = self.motion_packets()[-1]
        self.assertEqual(packet["data"]["id"], 1)
        self.assertEqual(packet["data"]["type"], 1)
        path = Path(packet["data"]["mtn"])
        self.assertTrue(path.is_relative_to(self.directory / "cache"))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_prepare_only_builds_local_motion_without_transport_or_files(self):
        self.renderer._performance_timer.start()
        self.assertTrue(self.prepare())
        self.assertTrue(self.renderer._speech_active)
        self.assertFalse(self.renderer._performance_timer.isActive())
        self.assertIsNotNone(self.renderer._timeline_motion)
        self.assertEqual(self.socket.packets, [])
        self.assertFalse((self.directory / "cache").exists())
        self.assertEqual(self.model.read_bytes(), self.model_before)
        self.assertEqual(hashlib.sha256(self.audio.read_bytes()).digest(), self.audio_hash)

    def test_processed_position_schedules_no_duplicate_window_below_1_6_seconds(self):
        self.assertTrue(self.prepare())
        self.assertTrue(self.renderer.sync_speech_position(0))
        for position in (0, .03, .5, 1, 1.59):
            self.assertFalse(self.renderer.sync_speech_position(position))
        self.assertEqual(len(self.motion_packets()), 1)
        self.assertTrue(self.renderer.sync_speech_position(1.6))
        self.assertEqual(len(self.motion_packets()), 2)
        self.assertEqual(self.last_motion()["Meta"]["Duration"], 2.)

    def test_windows_are_rebased_from_current_audio_position(self):
        self.assertTrue(self.prepare())
        original = copy.deepcopy(self.renderer._timeline_motion)
        self.assertTrue(self.renderer.sync_speech_position(1.2))
        window = self.last_motion()
        self.assertEqual(self.renderer._timeline_motion, original)
        for curve in window["Curves"]:
            source = curve_by_id(original, curve["Id"])
            self.assertAlmostEqual(points(curve)[0][1], sample(source, 1.2), places=5)

    def test_force_eof_sends_closed_tail_even_before_next_window_threshold(self):
        self.assertTrue(self.prepare())
        self.assertTrue(self.renderer.sync_speech_position(3.2))
        self.assertFalse(self.renderer.sync_speech_position(4))
        self.assertTrue(self.renderer.sync_speech_position(4, force=True))
        mouth = curve_by_id(self.last_motion(), "ParamMouthOpenY")
        self.assertTrue(all(value == 0 for _, value in points(mouth)))
        self.assertEqual(len(self.motion_packets()), 2)
        self.assertFalse(self.last_motion()["Meta"]["Loop"])

    def test_force_eof_at_exact_motion_end_cannot_reopen_mouth(self):
        self.assertTrue(self.prepare(duration=4))
        self.assertTrue(self.renderer.sync_speech_position(4, force=True))
        mouth = curve_by_id(self.last_motion(), "ParamMouthOpenY")
        self.assertTrue(all(value == 0 for _, value in points(mouth)))

    def test_pause_and_resume_discard_old_audio_clock_instead_of_replaying_speech(self):
        self.assertTrue(self.prepare())
        self.assertTrue(self.renderer.sync_speech_position(.3))
        self.renderer.set_paused(True)
        count = len(self.motion_packets())
        self.assertIsNone(self.renderer._timeline_motion)
        self.assertFalse(self.renderer.sync_speech_position(2, force=True))
        self.assertEqual(len(self.motion_packets()), count)
        paused = self.last_motion()
        self.assertTrue(paused["Meta"]["Loop"])
        self.assertTrue(all(value == 0 for _, value in points(curve_by_id(paused, "ParamMouthOpenY"))))
        self.renderer.set_paused(False)
        count = len(self.motion_packets())
        self.assertFalse(self.renderer.sync_speech_position(3))
        self.assertEqual(len(self.motion_packets()), count)
        self.assertFalse(self.renderer._speech_active)

    def test_disconnect_and_registration_reset_discard_old_clock(self):
        self.assertTrue(self.prepare())
        self.renderer._disconnected()
        self.assertIsNone(self.renderer._timeline_motion)
        count = len(self.motion_packets())
        self.assertFalse(self.renderer.sync_speech_position(2, force=True))
        self.assertEqual(len(self.motion_packets()), count)
        # A fresh transport registration cannot resurrect the previous clip.
        self.renderer._connected()
        self.renderer._registered = True
        self.renderer._managed_model_valid = True
        self.assertFalse(self.renderer.sync_speech_position(2, force=True))
        self.assertIsNone(self.renderer._timeline_motion)

    def test_finish_and_stop_reject_late_positions(self):
        self.assertTrue(self.prepare())
        self.renderer.finish_speech()
        count = len(self.motion_packets())
        self.assertFalse(self.renderer.sync_speech_position(2))
        self.assertEqual(len(self.motion_packets()), count)
        self.assertTrue(self.prepare())
        self.renderer.stop()
        count = len(self.motion_packets())
        self.assertFalse(self.renderer.sync_speech_position(2, force=True))
        self.assertEqual(len(self.motion_packets()), count)

    def test_invalid_positions_never_send_or_advance_scheduler(self):
        self.assertTrue(self.prepare())
        for invalid in (-.1, math.inf, math.nan, None, "bad"):
            with self.subTest(position=invalid):
                self.assertFalse(self.renderer.sync_speech_position(invalid))
        self.assertEqual(self.socket.packets, [])
        self.assertEqual(self.renderer._timeline_next_position, 0)

    def test_transport_failure_does_not_consume_the_next_audio_window(self):
        self.assertTrue(self.prepare())
        self.socket.reject = True
        self.assertFalse(self.renderer.sync_speech_position(0))
        self.assertEqual(self.renderer._timeline_next_position, 0)
        self.socket.reject = False
        self.assertTrue(self.renderer.sync_speech_position(0))
        self.assertEqual(self.renderer._timeline_next_position, 1.6)

    def test_motion_cache_limit_blocks_new_file_without_touching_user_files(self):
        self.assertTrue(self.prepare())
        user_file = self.directory / "cache" / "user-extension.txt"
        user_file.parent.mkdir(parents=True)
        user_file.write_bytes(b"keep extension")
        with patch("aipet.ui.live2d_renderer.MOTION_CACHE_LIMIT", 3), patch("aipet.ui.live2d_renderer.time.monotonic", return_value=100):
            for position in (0, 1.6, 3.2):
                self.assertTrue(self.renderer.sync_speech_position(position))
            self.assertFalse(self.renderer.sync_speech_position(4, force=True))
            self.assertEqual(len(self.renderer._motion_files), 3)
            self.assertEqual(len(list(self.renderer._motion_directory.glob("*.json"))), 3)
        self.assertEqual(user_file.read_bytes(), b"keep extension")
        self.assertEqual(MOTION_CACHE_LIMIT, 64)

    def test_expired_cache_files_are_pruned_but_recent_and_current_clip_retained(self):
        self.assertTrue(self.prepare())
        with patch("aipet.ui.live2d_renderer.time.monotonic", return_value=100):
            self.assertTrue(self.renderer.sync_speech_position(0))
            first = self.renderer._last_motion_file
        with patch("aipet.ui.live2d_renderer.time.monotonic", return_value=111):
            self.assertTrue(self.renderer.sync_speech_position(1.6))
            second = self.renderer._last_motion_file
            self.assertTrue(first.exists(), "Current Viewer file survives the prune before its replacement")
        with patch("aipet.ui.live2d_renderer.time.monotonic", return_value=122):
            self.assertTrue(self.renderer.sync_speech_position(3.2))
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
        self.assertLessEqual(len(self.renderer._motion_files), 2)

    def test_all_timeline_parameters_stay_in_presentation_allowlist_without_costume_or_sound(self):
        self.assertTrue(self.prepare())
        for position in (0, 1.6, 3.2, 4):
            self.assertTrue(self.renderer.sync_speech_position(position, force=True))
            motion = self.last_motion()
            self.assertNotIn("Sound", motion)
            self.assertNotIn("UserData", motion)
            for curve in motion["Curves"]:
                self.assertEqual(curve["Target"], "Parameter")
                self.assertIn(curve["Id"], PARAMETER_RANGES)
                low, high = PARAMETER_RANGES[curve["Id"]]
                self.assertTrue(all(low <= value <= high for _, value in points(curve)))
        self.assertEqual(set(packet["msg"] for packet in self.socket.packets), {13200})
        self.assertEqual(self.model.read_bytes(), self.model_before)
        self.assertEqual(hashlib.sha256(self.audio.read_bytes()).digest(), self.audio_hash)

    def test_prepare_rejects_wrong_slot_loading_disconnected_and_paused(self):
        for attribute, value in (("_managed_model_valid", False), ("_model_loading", True), ("_registered", False), ("_paused", True)):
            previous = getattr(self.renderer, attribute)
            setattr(self.renderer, attribute, value)
            with self.subTest(attribute=attribute):
                self.assertFalse(self.prepare())
                self.assertIsNone(self.renderer._timeline_motion)
            setattr(self.renderer, attribute, previous)
        wrong_slot = ViewerEXRenderer(ViewerEXConfig(self.directory / "port", model_path=self.model, slot=0), socket=FakeSocket())
        try:
            wrong_slot._registered = wrong_slot._managed_model_valid = True
            self.assertFalse(wrong_slot.prepare_timeline_speech("平静", self.audio, 4.5))
        finally:
            wrong_slot.stop()
        self.assertEqual(self.socket.packets, [])


if __name__ == "__main__":
    unittest.main()
