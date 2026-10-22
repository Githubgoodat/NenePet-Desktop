from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import PropertyMock, patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QObject, QPoint, QPointF, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QInputMethodEvent, QKeyEvent, QMouseEvent
from PyQt5.QtNetwork import QAbstractSocket, QHostAddress
from PyQt5.QtWebSockets import QWebSocketServer
from PyQt5.QtWidgets import QApplication

from aipet.core.backends import CharacterReply, CharacterSentence
from aipet.core.config import AppSettings
from aipet.core.live2d_performance import compose_motion
from aipet.core.workers import ConversationResult
from aipet.ui.live2d_renderer import (
    MOTION_NAMES,
    MOTION_ROOT,
    ViewerEXConfig,
    ViewerEXRenderer,
    validate_model3_path,
)
from aipet.ui.pet import Murasame


class FakeViewerSocket(QObject):
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    textMessageReceived = pyqtSignal(str)
    error = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.packets = []
        self.connection_state = QAbstractSocket.UnconnectedState

    def state(self):
        return self.connection_state

    def abort(self):
        self.connection_state = QAbstractSocket.UnconnectedState

    def open(self, url):
        self.connection_state = QAbstractSocket.ConnectedState
        self.connected.emit()

    def sendTextMessage(self, text):
        self.packets.append(json.loads(text))
        return len(text)

    def close(self):
        self.connection_state = QAbstractSocket.UnconnectedState
        self.disconnected.emit()


class ViewerEXTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # The Windows sandbox's redirected %TEMP% has different ACLs. Keep
        # disposable test profiles inside this writable checkout instead.
        self.directory = self.enterContext(tempfile.TemporaryDirectory(
            prefix=".test-viewerex-", dir=Path(__file__).resolve().parents[1],
        ))
        self.enterContext(patch.dict(os.environ, {"AIPET_CACHE_DIR": str(Path(self.directory) / "cache")}))

    def wait_for(self, condition, seconds=2):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(condition())

    def write_model_export(self):
        root = Path(self.directory) / "export"
        root.mkdir(exist_ok=True)
        references = {"Moc": "model.moc3", "Textures": ["texture.png"],
                      "Physics": "model.physics3.json", "DisplayInfo": "model.cdi3.json"}
        for name in ("model.moc3", "texture.png", "model.physics3.json", "model.cdi3.json"):
            (root / name).write_bytes(b"local dependency fixture; never rendered")
        entry = root / "nene.model3.json"
        entry.write_text(json.dumps({"Version": 3, "FileReferences": references}), encoding="utf-8")
        return entry

    @unittest.skip('Legacy local nene_main.py is deliberately excluded; public bootstrap is covered separately.')
    def test_nene_entry_selects_stable_export_without_creating_profile_files(self):
        import nene_main
        root = Path(self.directory)
        with patch.object(nene_main, "ROOT", root), patch.dict(os.environ):
            nene_main.configure_profile(create=False)
            config = ViewerEXConfig.from_environment()
            self.assertEqual(config.model_path, root / "assets/models/nene-editable/nene.model3.json")
            self.assertEqual(config.slot, 1)
            self.assertFalse((root / "data").exists())

    def test_standard_export_validation_accepts_optional_absence_and_checks_all_dependencies(self):
        entry = self.write_model_export()
        self.assertEqual(validate_model3_path(entry), entry.resolve())
        payload = json.loads(entry.read_text(encoding="utf-8"))
        payload["FileReferences"]["Expressions"] = [{"Name": "example", "File": "face.exp3.json"}]
        payload["FileReferences"]["Motions"] = {"Idle": [{"File": "idle.motion3.json", "Sound": "idle.wav"}]}
        for name in ("face.exp3.json", "idle.motion3.json", "idle.wav"):
            (entry.parent / name).write_bytes(b"not executed")
        entry.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(validate_model3_path(entry), entry.resolve())
        for name in ("model.moc3", "texture.png", "model.physics3.json", "model.cdi3.json",
                     "face.exp3.json", "idle.motion3.json", "idle.wav"):
            with self.subTest(dependency=name):
                dependency = entry.parent / name
                saved = dependency.read_bytes()
                dependency.unlink()
                with self.assertRaisesRegex(ValueError, "缺失"):
                    validate_model3_path(entry)
                dependency.write_bytes(saved)

    def test_invalid_or_incomplete_export_never_replaces_existing_model(self):
        entry = self.write_model_export()
        valid = entry.read_bytes()
        for invalid in (b"not json", b"[]", b'{"Version":2}',
                        b'{"Version":3,"FileReferences":{}}'):
            with self.subTest(invalid=invalid):
                entry.write_bytes(invalid)
                renderer = ViewerEXRenderer(ViewerEXConfig(Path("missing"), model_path=entry))
                warnings = []
                renderer.warning.connect(warnings.append)
                try:
                    with patch.object(renderer, "_send") as send:
                        renderer._received('{"msg":10000}')
                        send.assert_not_called()
                    self.assertIn("保留 Viewer 当前模型", warnings[0])
                    self.assertIsNone(renderer._model_load_timer)
                finally:
                    renderer.stop()
        entry.write_bytes(valid)
        (entry.parent / "texture.png").unlink()
        for path in (entry, entry.parent / "missing.model3.json", entry.with_suffix(".cmo3")):
            renderer = ViewerEXRenderer(ViewerEXConfig(Path("missing"), model_path=path))
            warnings = []
            renderer.warning.connect(warnings.append)
            try:
                with patch.object(renderer, "_send") as send:
                    renderer._received('{"msg":10000}')
                    send.assert_not_called()
                self.assertTrue(warnings)
            finally:
                renderer.stop()

    def test_export_loading_is_restricted_to_nene_slot(self):
        entry = self.write_model_export()
        with patch.dict(os.environ, {"AIPET_VIEWER_MODEL_PATH": str(entry), "AIPET_VIEWER_SLOT": "0"}):
            with self.assertRaisesRegex(ValueError, "Nene's slot 1"):
                ViewerEXConfig.from_environment()
        renderer = ViewerEXRenderer(ViewerEXConfig(Path("missing"), slot=0, model_path=entry))
        try:
            with patch.object(renderer, "_send") as send:
                renderer._received('{"msg":10000}')
                send.assert_not_called()
        finally:
            renderer.stop()

    @unittest.skip('Real loopback WebSocket integration is excluded from the zero-network CI suite.')
    def test_local_export_real_loopback_defers_position_and_coalesces_latest_emotion(self):
        entry = self.write_model_export()
        server = QWebSocketServer("local-export-test", QWebSocketServer.NonSecureMode)
        self.assertTrue(server.listen(QHostAddress.LocalHost, 0))
        peers, packets = [], []

        def connected():
            peer = server.nextPendingConnection()
            peers.append(peer)

            def receive(text):
                packet = json.loads(text)
                packets.append(packet)
                if packet["msg"] == 10000:
                    peer.sendTextMessage(json.dumps(packet))
            peer.textMessageReceived.connect(receive)

        server.newConnection.connect(connected)
        renderer = ViewerEXRenderer(ViewerEXConfig(
            Path(self.directory) / "no-port-file", port=server.serverPort(), model_path=entry,
            position=(900, -350), managed_position=True,
        ))
        statuses = []
        renderer.status_changed.connect(lambda connected, text: statuses.append(text))
        try:
            with patch("aipet.ui.live2d_renderer.MODEL_LOAD_SETTLE_MS", 150):
                renderer.start()
                self.wait_for(lambda: any(packet["msg"] == 13000 for packet in packets))
                self.assertEqual([packet["msg"] for packet in packets], [10000, 13000])
                self.assertEqual(packets[1]["data"], {"id": 1, "file": str(entry.resolve())})
                self.assertFalse(renderer.set_emotion("高兴"))
                self.assertFalse(renderer.set_emotion("害羞"))
                self.assertFalse(renderer.begin_drag(QPoint(10, 20)))
                self.wait_for(lambda: len(packets) == 4)
            self.assertEqual([packet["msg"] for packet in packets], [10000, 13000, 13400, 13200])
            self.assertEqual(packets[2]["data"], {"id": 1, "posX": 900, "posY": -350})
            data = packets[3]["data"]
            self.assertEqual((data["id"], data["type"]), (1, 1))
            path = Path(data["mtn"])
            self.assertTrue(path.is_relative_to(Path(self.directory) / "cache/live2d-performance"))
            self.assertEqual(renderer._base_emotion, "害羞")
            self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["Meta"]["Loop"])
            self.assertTrue(any("画面待核对" in text for text in statuses))
            self.assertFalse(any("加载完成" in text for text in statuses))
        finally:
            renderer.stop()
            for peer in peers:
                peer.close()
            server.close()

    def test_disconnect_stop_and_stale_timer_cannot_send_into_another_session(self):
        renderer = ViewerEXRenderer(ViewerEXConfig(
            Path("missing"), model_path=self.write_model_export(),
        ))
        try:
            with (
                patch.object(ViewerEXRenderer, "ready", new_callable=PropertyMock, return_value=True),
                patch.object(renderer, "_send", return_value=True) as send,
            ):
                renderer._received('{"msg":10000}')
                old_generation = renderer._model_generation
                old_timer = renderer._model_load_timer
                renderer._received('{"msg":10000}')
                self.assertEqual(send.call_count, 1)  # Duplicate ACK does not reload.
                renderer._disconnected()
                self.assertFalse(old_timer.isActive())
                self.assertIsNone(renderer._model_load_timer)
                renderer._received('{"msg":10000}')
                self.assertEqual(send.call_count, 2)
                generation = renderer._model_generation
                timer = renderer._model_load_timer
                send.reset_mock()
                renderer._finish_model_wait(old_generation)
                send.assert_not_called()
                self.assertIs(renderer._model_load_timer, timer)
                renderer._finish_model_wait(generation)
                self.assertEqual(send.call_args.args[0], 13200)
                self.assertEqual(renderer._base_emotion, "平静")
                self.assertTrue(Path(send.call_args.args[1]["mtn"]).is_file())
                renderer._disconnected()
                renderer._received('{"msg":10000}')
                generation = renderer._model_generation
                timer = renderer._model_load_timer
                renderer.stop()
                self.assertFalse(timer.isActive())
                send.reset_mock()
                renderer._finish_model_wait(generation)
                send.assert_not_called()
        finally:
            renderer.stop()

    def test_failed_load_send_does_not_arm_timer_or_send_position_and_face(self):
        renderer = ViewerEXRenderer(ViewerEXConfig(
            Path("missing"), model_path=self.write_model_export(),
            position=(900, -350), managed_position=True,
        ))
        try:
            with patch.object(renderer, "_send", return_value=False) as send:
                renderer._received('{"msg":10000}')
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[0], 13000)
                self.assertIsNone(renderer._model_load_timer)
                self.assertFalse(renderer._model_loading)
        finally:
            renderer.stop()

    def test_default_geometry_matches_observed_desktop(self):
        config = ViewerEXConfig(Path("missing"))
        layout = config.layout(QRect(0, 0, 2560, 1440))
        self.assertEqual(
            layout.body_rect.translated(layout.geometry.topLeft()),
            QRect(2100, 750, 400, 640),
        )
        self.assertEqual(layout.geometry, QRect(2000, 490, 560, 900))
        self.assertLess(layout.dialogue_rect.bottom(), layout.body_rect.top())

    def test_rejects_invalid_calibration_and_reads_dynamic_port(self):
        for value in ("0,0,-40,640", "nan,0,400,640", "0,0,400"):
            with self.subTest(value=value), patch.dict(os.environ, {"AIPET_VIEWER_BBOX": value}):
                with self.assertRaises(ValueError):
                    ViewerEXConfig.from_environment()
        port_file = Path(self.directory) / "app_port"
        config = ViewerEXConfig(port_file, port=10086)
        self.assertEqual(config.discover_port(), 10086)
        port_file.write_text("12345", encoding="utf-8-sig")
        self.assertEqual(config.discover_port(), 12345)
        port_file.write_text("99999", encoding="utf-8")
        self.assertEqual(config.discover_port(), 10086)

    def test_reference_and_stale_calibrations_fit_changed_resolution(self):
        for screen in (
            QRect(0, 0, 1856, 1075),
            QRect(0, 0, 1920, 1080),
            QRect(-1920, 0, 1920, 1080),
            QRect(2560, 120, 1280, 720),
            QRect(0, 0, 80, 60),
        ):
            for bbox in (None, (2100, 750, 400, 640), (10000, -9000, 4000, 3000)):
                with self.subTest(screen=screen, bbox=bbox):
                    layout = ViewerEXConfig(Path("missing"), bbox=bbox).layout(screen)
                    self.assertTrue(screen.contains(layout.geometry))
                    self.assertTrue(QRect(QPoint(), layout.geometry.size()).contains(layout.body_rect))
                    self.assertTrue(QRect(QPoint(), layout.geometry.size()).contains(layout.dialogue_rect))
        screen = QRect(0, 0, 1856, 1075)
        layout = ViewerEXConfig(Path("missing"), bbox=(2100, 750, 400, 640)).layout(screen)
        self.assertEqual(
            layout.body_rect.translated(layout.geometry.topLeft()),
            QRect(1522, 560, 290, 478),
        )

    def test_visible_custom_calibration_is_not_rescaled(self):
        bbox = (1300, 500, 300, 550)
        layout = ViewerEXConfig(Path("missing"), bbox=bbox).layout(QRect(0, 0, 1856, 1075))
        self.assertEqual(layout.body_rect.translated(layout.geometry.topLeft()), QRect(*bbox))

    @unittest.skip('Real loopback WebSocket integration is excluded from the zero-network CI suite.')
    def test_real_websocket_registration_faces_and_explicit_drag(self):
        server = QWebSocketServer("adapter-test", QWebSocketServer.NonSecureMode)
        self.assertTrue(server.listen(QHostAddress.LocalHost, 0))
        peers = []
        packets = []

        def connected():
            peer = server.nextPendingConnection()
            peers.append(peer)

            def receive(text):
                packet = json.loads(text)
                packets.append(packet)
                if packet["msg"] == 10000:
                    peer.sendTextMessage(json.dumps(packet))

            peer.textMessageReceived.connect(receive)

        server.newConnection.connect(connected)
        renderer = ViewerEXRenderer(ViewerEXConfig(
            Path("missing"), port=server.serverPort(), position=(900, -350),
        ))
        try:
            renderer.start()
            self.wait_for(lambda: renderer.ready)
            self.assertEqual([packet["msg"] for packet in packets], [10000])
            self.assertTrue(renderer.set_emotion("害羞"))
            self.wait_for(lambda: len(packets) == 2)
            self.assertEqual(packets[-1]["data"], {
                "id": 1, "type": 1,
                "mtn": str((MOTION_ROOT / "shy.motion3.json").resolve()),
            })
            self.assertTrue(renderer.begin_drag(QPoint(100, 200)))
            self.assertTrue(renderer.drag_to(QPoint(135, 220)))
            self.wait_for(lambda: len(packets) == 3)
            self.assertEqual(packets[-1]["data"], {"id": 1, "posX": 935, "posY": -370})
            renderer.end_drag()
            renderer._connected()
            self.wait_for(lambda: len(packets) == 4)
            self.assertEqual(packets[-1]["msg"], 10000)
            self.assertEqual(sum(packet["msg"] == 13400 for packet in packets), 1)
        finally:
            renderer.stop()
            for peer in peers:
                peer.close()
            server.close()

    def test_uncalibrated_drag_never_moves_even_when_connected(self):
        renderer = ViewerEXRenderer(ViewerEXConfig(Path("missing")))
        warnings = []
        renderer.warning.connect(warnings.append)
        try:
            with patch.object(renderer, "_send") as send:
                self.assertFalse(renderer.begin_drag(QPoint(100, 200)))
                self.assertFalse(renderer.drag_to(QPoint(135, 220)))
                send.assert_not_called()
            self.assertEqual(len(warnings), 1)
        finally:
            renderer.stop()

    def test_initial_viewer_failure_is_visible_without_repeated_or_shutdown_warnings(self):
        renderer = ViewerEXRenderer(ViewerEXConfig(Path("missing")))
        warnings = []
        renderer.warning.connect(warnings.append)
        renderer._disconnected()
        renderer._disconnected()
        self.assertEqual(len(warnings), 1)
        self.assertIn("文字输入仍可使用", warnings[0])
        self.assertIn("表情和同步拖动暂不可用", warnings[0])
        renderer.stop()
        renderer._disconnected()
        self.assertEqual(len(warnings), 1)
        with patch.object(renderer, "_send") as send:
            renderer._received('{"msg":10000,"msgId":9}')
            self.assertFalse(renderer.ready)
            send.assert_not_called()

    def test_managed_registration_and_reconnect_use_latest_known_position(self):
        renderer = ViewerEXRenderer(ViewerEXConfig(
            Path("missing"), position=(900, -350), managed_position=True,
        ))
        try:
            with (
                patch.object(ViewerEXRenderer, "ready", new_callable=PropertyMock, return_value=True),
                patch.object(renderer, "_send", return_value=True) as send,
            ):
                renderer._received('{"msg":10000,"msgId":1}')
                send.assert_called_once_with(13400, {"id": 1, "posX": 900, "posY": -350})
                self.assertTrue(renderer.begin_drag(QPoint(100, 200)))
                self.assertTrue(renderer.drag_to(QPoint(135, 220)))
                renderer.end_drag()
                send.reset_mock()
                renderer._disconnected()
                renderer._received('{"msg":10000,"msgId":2}')
                send.assert_called_once_with(13400, {"id": 1, "posX": 935, "posY": -370})
        finally:
            renderer.stop()

    def test_registered_model_tap_is_relayed_without_other_viewer_commands(self):
        renderer = ViewerEXRenderer(ViewerEXConfig(Path("missing"), slot=1))
        tapped = []
        renderer.model_tapped.connect(lambda: tapped.append(True))
        try:
            with (
                patch.object(ViewerEXRenderer, "ready", new_callable=PropertyMock, return_value=True),
                patch.object(renderer, "_send") as send,
            ):
                renderer._received(json.dumps({"msg": 10002, "data": {"type": 0, "id": 1, "hitArea": "body"}}))
                self.assertEqual(tapped, [True])
                send.assert_not_called()
        finally:
            renderer.stop()

    def test_other_slots_malformed_events_and_disconnected_taps_do_not_focus_input(self):
        renderer = ViewerEXRenderer(ViewerEXConfig(Path("missing"), slot=1))
        tapped = []
        renderer.model_tapped.connect(lambda: tapped.append(True))
        try:
            valid = {"msg": 10002, "data": {"type": 0, "id": 1}}
            renderer._received(json.dumps(valid))  # No registered connection.
            with patch.object(ViewerEXRenderer, "ready", new_callable=PropertyMock, return_value=True):
                for data in ({"type": 0, "id": 0}, {"type": 1, "id": 1},
                             {"type": False, "id": 1}, {"type": 0, "id": True},
                             {"type": "0", "id": 1}, {}, None, []):
                    renderer._received(json.dumps({"msg": 10002, "data": data}))
                renderer._received("[]")
                renderer._received("not json")
                renderer.stop()
                renderer._received(json.dumps(valid))
            self.assertEqual(tapped, [])
        finally:
            renderer.stop()

    def test_managed_mode_without_position_or_wrong_slot_cannot_move(self):
        for config in (
            ViewerEXConfig(Path("missing"), managed_position=True),
            ViewerEXConfig(Path("missing"), managed_position=True, position=(900, -350), slot=0),
        ):
            renderer = ViewerEXRenderer(config)
            try:
                with patch.object(renderer, "_send") as send:
                    renderer._received('{"msg":10000,"msgId":1}')
                    self.assertFalse(renderer.begin_drag(QPoint(100, 200)))
                    self.assertFalse(renderer.drag_to(QPoint(135, 220)))
                    send.assert_not_called()
            finally:
                renderer.stop()
        with patch.dict(os.environ, {"AIPET_VIEWER_MANAGED_POSITION": "1", "AIPET_VIEWER_SLOT": "0"}):
            with self.assertRaises(ValueError):
                ViewerEXConfig.from_environment()

    def test_packaged_motions_only_touch_allowlisted_face_parameters(self):
        allowed = {
            "ParamAngleZ", "ParamEyeLOpen", "ParamEyeROpen", "ParamEyeLSmile",
            "ParamEyeRSmile", "ParamBrowLY", "ParamBrowRY", "ParamBrowLAngle",
            "ParamBrowRAngle", "ParamMouthForm", "ParamMouthOpenY", "ParamCheek",
        }
        for name in MOTION_NAMES.values():
            with self.subTest(name=name):
                motion = json.loads((MOTION_ROOT / f"{name}.motion3.json").read_text(encoding="utf-8"))
                self.assertFalse(motion["Meta"]["Loop"])
                for curve in motion["Curves"]:
                    self.assertEqual(curve["Target"], "Parameter")
                    self.assertIn(curve["Id"], allowed)

    def performance_renderer(self, *, finish_loading=True):
        socket = FakeViewerSocket()
        renderer = ViewerEXRenderer(ViewerEXConfig(
            Path("missing"), model_path=self.write_model_export(),
        ), socket=socket)
        self.addCleanup(renderer.stop)
        renderer.start()
        socket.textMessageReceived.emit('{"msg":10000}')
        if finish_loading:
            renderer._finish_model_wait(renderer._model_generation)
        return renderer, socket

    @staticmethod
    def motion_packets(socket):
        return [packet for packet in socket.packets if packet["msg"] == 13200]

    def last_motion(self, socket):
        path = Path(self.motion_packets(socket)[-1]["data"]["mtn"])
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def curve_values(motion, name):
        segments = next(curve["Segments"] for curve in motion["Curves"] if curve["Id"] == name)
        return segments[1::3]

    def make_voice_wave(self):
        path = Path(self.directory) / "voice.wav"
        rate = 16000
        samples = np.zeros(rate * 2, dtype="<i2")
        voiced = np.arange(rate) / rate
        samples[rate // 2: rate + rate // 2] = (np.sin(2 * np.pi * 220 * voiced) * 9000).astype("<i2")
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(samples.tobytes())
        return path

    def test_loading_coalesces_base_attention_and_discards_speech_request(self):
        renderer, socket = self.performance_renderer(finish_loading=False)
        self.assertFalse(renderer.set_base_emotion("高兴", .3))
        self.assertFalse(renderer.set_base_emotion("生气", .7))
        self.assertFalse(renderer.set_activity("listening", attention=(.2, -.3)))
        self.assertFalse(renderer.play_speech("害羞", self.make_voice_wave(), 2))
        self.assertEqual(self.motion_packets(socket), [])
        renderer._finish_model_wait(renderer._model_generation)
        self.assertEqual(len(self.motion_packets(socket)), 1)
        self.assertEqual(renderer._base_emotion, "生气")
        self.assertEqual(renderer._activity, "listening")
        self.assertEqual(set(self.curve_values(self.last_motion(socket), "ParamMouthOpenY")), {0.0})
        self.assertFalse(renderer._speech_active)
        self.assertTrue(renderer._performance_timer.isActive())

    def test_mood_sync_is_idempotent_and_refresh_has_new_small_motion(self):
        renderer, socket = self.performance_renderer()
        renderer.set_base_emotion("生气", .7)
        before = len(self.motion_packets(socket))
        first = self.last_motion(socket)
        self.assertFalse(renderer.set_base_emotion("生气", .7))
        renderer.set_paused(False)
        self.assertFalse(renderer.set_activity("idle", attention=(0, 0)))
        self.assertEqual(len(self.motion_packets(socket)), before)
        renderer._refresh_performance()
        second = self.last_motion(socket)
        self.assertEqual(renderer._base_emotion, "生气")
        self.assertNotEqual(first["Curves"], second["Curves"])
        self.assertEqual(set(self.curve_values(second, "ParamMouthOpenY")), {0.0})
        self.assertTrue(all(packet["data"]["id"] == 1 for packet in self.motion_packets(socket)))

    def test_speech_is_finite_and_idle_attention_and_mood_cannot_preempt_it(self):
        renderer, socket = self.performance_renderer()
        self.assertTrue(renderer.play_speech("高兴", self.make_voice_wave(), 4))
        before = len(self.motion_packets(socket))
        speech = self.last_motion(socket)
        mouth = self.curve_values(speech, "ParamMouthOpenY")
        self.assertFalse(speech["Meta"]["Loop"])
        self.assertEqual(speech["Meta"]["Duration"], 4)
        self.assertGreater(max(mouth), .1)
        self.assertEqual(mouth[-1], 0)
        segments = next(curve["Segments"] for curve in speech["Curves"] if curve["Id"] == "ParamMouthOpenY")
        times = [segments[0]] + segments[3::3]
        self.assertTrue(all(value == 0 for time, value in zip(times, mouth) if time >= 2))
        renderer.set_base_emotion("生气", .7)
        renderer.set_activity("thinking", attention=(-.2, .1))
        renderer._refresh_performance()
        self.assertEqual(len(self.motion_packets(socket)), before)
        self.assertFalse(renderer._performance_timer.isActive())
        renderer.finish_speech()
        self.assertEqual(len(self.motion_packets(socket)), before + 1)
        self.assertEqual(renderer._emotion, "生气")
        self.assertEqual(renderer._activity, "thinking")
        self.assertEqual(set(self.curve_values(self.last_motion(socket), "ParamMouthOpenY")), {0.0})
        renderer.finish_speech()
        self.assertEqual(len(self.motion_packets(socket)), before + 1)

    def test_pause_closes_mouth_and_loops_only_silent_blink_breath_until_resume(self):
        renderer, socket = self.performance_renderer()
        renderer.set_base_emotion("生气", .7)
        renderer.set_activity("thinking", attention=(-.2, .1))
        renderer.play_speech("高兴", self.make_voice_wave(), 2)
        renderer.set_paused(True)
        paused = self.last_motion(socket)
        self.assertTrue(paused["Meta"]["Loop"])
        self.assertFalse(renderer._speech_active)
        self.assertFalse(renderer._performance_timer.isActive())
        self.assertEqual(set(self.curve_values(paused, "ParamMouthOpenY")), {0.0})
        for curve in paused["Curves"]:
            values = self.curve_values(paused, curve["Id"])
            if curve["Id"] not in {"ParamBreath", "ParamEyeLOpen", "ParamEyeROpen"}:
                self.assertEqual(len(set(values)), 1, curve["Id"])
        before = len(self.motion_packets(socket))
        renderer.set_activity("thinking", attention=(.5, .5))
        renderer.set_base_emotion("着急", .4)
        renderer.set_paused(True)
        self.assertFalse(renderer.play_speech("高兴", self.make_voice_wave(), 2))
        self.assertEqual(len(self.motion_packets(socket)), before)
        renderer.set_paused(False)
        self.assertEqual(renderer._emotion, "着急")
        self.assertEqual((renderer._activity, renderer._attention), ("idle", (0.0, 0.0)))
        self.assertEqual(len(self.motion_packets(socket)), before + 1)
        self.assertFalse(self.last_motion(socket)["Meta"]["Loop"])
        self.assertTrue(renderer._performance_timer.isActive())

    def test_reconnect_drops_audio_and_restores_latest_base_activity(self):
        renderer, socket = self.performance_renderer()
        renderer.play_speech("高兴", self.make_voice_wave(), 2)
        renderer.set_base_emotion("害羞", .4)
        renderer.set_activity("listening", attention=(0, .2))
        socket.close()
        self.assertFalse(renderer._speech_active)
        self.assertFalse(renderer._performance_timer.isActive())
        self.assertFalse(renderer.play_speech("高兴", self.make_voice_wave(), 2))
        renderer._connect_if_needed()
        socket.textMessageReceived.emit('{"msg":10000}')
        renderer._finish_model_wait(renderer._model_generation)
        self.assertEqual(renderer._emotion, "害羞")
        self.assertEqual(renderer._activity, "listening")
        self.assertEqual(set(self.curve_values(self.last_motion(socket), "ParamMouthOpenY")), {0.0})

    def test_bad_or_overlong_audio_has_closed_mouth_without_reading_or_deleting_audio(self):
        renderer, socket = self.performance_renderer()
        broken = Path(self.directory) / "broken.wav"
        broken.write_bytes(b"invalid user-owned wave")
        with patch("aipet.ui.live2d_renderer.LOGGER"):
            self.assertTrue(renderer.play_speech("生气", broken, 2))
        self.assertEqual(broken.read_bytes(), b"invalid user-owned wave")
        self.assertEqual(set(self.curve_values(self.last_motion(socket), "ParamMouthOpenY")), {0.0})
        renderer.finish_speech()
        with patch("aipet.ui.live2d_renderer.compose_motion", wraps=compose_motion) as compose:
            self.assertTrue(renderer.play_speech("生气", broken, 121))
        self.assertIsNone(compose.call_args.kwargs["audio_path"])
        self.assertEqual(set(self.curve_values(self.last_motion(socket), "ParamMouthOpenY")), {0.0})

    def test_cache_bound_refuses_generation_instead_of_deleting_recent_viewer_files(self):
        renderer, socket = self.performance_renderer()
        with patch("aipet.ui.live2d_renderer.MOTION_CACHE_LIMIT", 3):
            self.assertTrue(renderer._refresh_performance())
            self.assertTrue(renderer._refresh_performance())
            paths = [Path(packet["data"]["mtn"]) for packet in self.motion_packets(socket)]
            before = len(self.motion_packets(socket))
            self.assertFalse(renderer._refresh_performance())
            self.assertEqual(len(self.motion_packets(socket)), before)
            self.assertEqual(len(renderer._motion_files), 3)
            self.assertTrue(all(path.is_file() for path in paths))
        with patch("aipet.ui.live2d_renderer.time.monotonic", return_value=time.monotonic() + 180):
            self.assertTrue(renderer._refresh_performance())
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertTrue(paths[-1].exists())  # Last requested file remains protected.

    def test_stop_protects_recent_send_and_user_files_and_blocks_every_late_callback(self):
        renderer, socket = self.performance_renderer()
        renderer.set_base_emotion("生气", .7)
        paths = [Path(packet["data"]["mtn"]) for packet in self.motion_packets(socket)]
        sentinel = paths[-1].parent / "user-notes.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with patch("aipet.ui.live2d_renderer.QTimer.singleShot") as later:
            renderer.stop()
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[-1].exists())  # No asynchronous Viewer read race.
        self.assertEqual(later.call_args.args[0], 30000)
        self.assertFalse(renderer._performance_timer.isActive())
        self.assertFalse(renderer._reconnect_timer.isActive())
        before = len(socket.packets)
        renderer._refresh_performance()
        renderer.set_activity("listening")
        renderer.set_base_emotion("高兴")
        renderer.set_paused(True)
        renderer.finish_speech()
        renderer.play_speech("高兴", self.make_voice_wave(), 2)
        renderer._connected()
        renderer._received('{"msg":10000}')
        renderer._send(13200, {})
        self.assertEqual(len(socket.packets), before)
        later.call_args.args[1]()
        self.assertFalse(paths[-1].exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_invalid_export_never_animates_existing_unknown_rig(self):
        entry = self.write_model_export()
        entry.write_text("{}", encoding="utf-8")
        socket = FakeViewerSocket()
        renderer = ViewerEXRenderer(ViewerEXConfig(Path("missing"), model_path=entry), socket=socket)
        self.addCleanup(renderer.stop)
        renderer.start()
        socket.textMessageReceived.emit('{"msg":10000}')
        renderer.set_base_emotion("生气")
        renderer.set_activity("thinking")
        renderer.play_speech("高兴", self.make_voice_wave(), 2)
        renderer.set_paused(True)
        renderer._refresh_performance()
        self.assertEqual(self.motion_packets(socket), [])
        self.assertIsNone(renderer._motion_directory)

    def test_unwritable_cache_fails_presentation_safely_and_recovers_after_path_is_fixed(self):
        renderer, socket = self.performance_renderer(finish_loading=False)
        occupied = Path(self.directory) / "occupied-cache"
        occupied.write_bytes(b"user owned")
        with (patch("aipet.ui.live2d_renderer.get_cache_dir", return_value=occupied),
              patch("aipet.ui.live2d_renderer.LOGGER")):
            renderer._finish_model_wait(renderer._model_generation)
            self.assertFalse(renderer.play_speech("高兴", self.make_voice_wave(), 2))
        self.assertIsNone(renderer._motion_directory)
        self.assertEqual(self.motion_packets(socket), [])
        self.assertEqual(occupied.read_bytes(), b"user owned")
        self.assertTrue(renderer.play_speech("高兴", self.make_voice_wave(), 2))
        self.assertEqual(len(self.motion_packets(socket)), 1)

    def make_pet(self, directory, *, managed=False, saved_position=None,
                 profile_id="murasame", language="zh-CN", name="", name_en=""):
        environment = {
            "AIPET_RENDERER": "viewerex", "AIPET_DATA_DIR": directory,
            "AIPET_VIEWER_BBOX": "400,250,180,300",
            "AIPET_VIEWER_OVERLAY_WIDTH": "400", "AIPET_VIEWER_DIALOGUE_HEIGHT": "150",
            "AIPET_CHARACTER_NAME": "宁宁",
            "AIPET_VIEWER_POSITION": "900,-350" if managed else "",
            "AIPET_VIEWER_MANAGED_POSITION": "1" if managed else "",
        }
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        settings = AppSettings(ui_language=language)
        settings.character.profile_id = profile_id
        settings.character.name = name
        settings.character.name_en = name_en
        settings.tts.enabled = False
        settings.vision.enabled = False
        if saved_position is not None:
            settings.display.window_x, settings.display.window_y = saved_position
        with patch.object(ViewerEXRenderer, "start"):
            pet = Murasame(settings)
            self.app.processEvents()
        self.addCleanup(pet.shutdown)
        self.addCleanup(pet.close)
        return pet

    def test_window_identity_follows_character_name_and_ui_language(self):
        pet = self.make_pet(
            self.directory, profile_id="nene", name="宁宁", name_en="Nene",
        )
        flags = pet.windowFlags()
        self.assertEqual(pet.windowTitle(), "宁宁 · 桌宠")
        self.assertEqual(pet.accessibleName(), "宁宁 · 桌宠")
        self.assertIn("身体输入", pet.accessibleDescription())
        self.assertIn("中键", pet.accessibleDescription())
        self.assertEqual(pet.windowType(), Qt.Tool)
        self.assertTrue(pet.testAttribute(Qt.WA_TranslucentBackground))

        updated = pet.settings.model_copy(deep=True)
        updated.ui_language = "en"
        pet.apply_settings(updated)
        self.assertEqual(pet.windowTitle(), "Nene · Desktop Pet")
        self.assertEqual(pet.accessibleName(), "Nene · Desktop Pet")
        self.assertIn("body to type", pet.accessibleDescription())
        self.assertIn("middle mouse button", pet.accessibleDescription())

        updated.character.name_en = "Test Nene"
        pet.apply_settings(updated)
        self.assertEqual(pet.windowTitle(), "Test Nene · Desktop Pet")
        self.assertEqual(pet.accessibleName(), pet.windowTitle())
        updated.ui_language = "zh-CN"
        updated.character.name = "测试宁宁"
        pet.apply_settings(updated)
        self.assertEqual(pet.windowTitle(), "测试宁宁 · 桌宠")
        self.assertEqual(pet.accessibleName(), pet.windowTitle())
        self.assertEqual(pet.windowFlags(), flags)
        self.assertTrue(pet.testAttribute(Qt.WA_TranslucentBackground))

    def test_original_body_input_ime_submit_and_head_pat_survive(self):
        pet = self.make_pet(self.directory)
        body = pet._viewer_layout.body_rect
        lower = QPointF(body.center().x(), body.y() + body.height() * .8)
        click = QMouseEvent(QMouseEvent.MouseButtonPress, lower, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        pet.mousePressEvent(click)
        self.assertTrue(pet.input_mode)
        event = QInputMethodEvent()
        event.setCommitString("宁宁，下午好")
        pet.inputMethodEvent(event)
        self.assertIn("宁宁，下午好", pet.input_buffer)
        with patch.object(pet, "start_thread") as start:
            pet.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
            start.assert_called_once_with("宁宁，下午好", role="user")
        head = QPointF(body.center().x(), body.y() + 12)
        pet.mousePressEvent(QMouseEvent(QMouseEvent.MouseButtonPress, head, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        with patch.object(pet, "start_thread") as start:
            pet.mouseMoveEvent(QMouseEvent(QMouseEvent.MouseMove, head + QPointF(60, 0), Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
            self.assertEqual(start.call_args.kwargs, {"role": "system"})

    def test_viewer_tap_signal_opens_real_qt_ime_editor_without_losing_a_draft(self):
        pet = self.make_pet(self.directory, profile_id="nene", name="宁宁")
        # No coordinate is used: the Viewer can be in another position/window.
        pet._viewerex.model_tapped.emit()
        self.assertTrue(pet.input_mode)
        committed = QInputMethodEvent()
        committed.setCommitString("你好，宁宁")
        pet.inputMethodEvent(committed)
        pet.inputMethodEvent(QInputMethodEvent("ning", []))
        display = pet.display_text
        pet._viewerex.model_tapped.emit()
        self.assertEqual(pet.input_buffer, "你好，宁宁")
        self.assertEqual(pet.preedit_text, "ning")
        self.assertEqual(pet.display_text, display)
        pet.inputMethodEvent(QInputMethodEvent("", []))
        with patch.object(pet, "start_thread") as start:
            pet.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
            start.assert_called_once_with("你好，宁宁", role="user")

    def test_nene_head_pat_and_empty_enter_use_configured_name_without_master(self):
        for language, name in (("zh-CN", "测试宁宁"), ("en", "Test Nene")):
            with self.subTest(language=language):
                pet = self.make_pet(
                    self.directory, profile_id="nene", language=language,
                    name="测试宁宁", name_en="Test Nene",
                )
                body = pet._viewer_layout.body_rect
                head = QPointF(body.center().x(), body.y() + 12)
                pet.mousePressEvent(QMouseEvent(
                    QMouseEvent.MouseButtonPress, head, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
                ))
                with patch.object(pet, "start_thread") as start:
                    pet.mouseMoveEvent(QMouseEvent(
                        QMouseEvent.MouseMove, head + QPointF(60, 0), Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
                    ))
                    event_text = start.call_args.args[0]
                    self.assertIn(name, event_text)
                    self.assertNotIn("主人", event_text)
                    self.assertNotIn("Master", event_text)
                    self.assertEqual(start.call_args.kwargs, {"role": "system"})
                pet.input_mode = True
                pet.input_buffer = ""
                with patch.object(pet, "show_text") as show:
                    pet.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
                    prompt = show.call_args.args[0]
                    self.assertNotIn("主人", prompt)
                    self.assertNotIn("Master", prompt)
                    self.assertTrue(prompt)
                self.assertFalse(pet.input_mode)

    def test_default_murasame_head_pat_and_empty_enter_keep_original_address(self):
        for language, head_text, empty_text in (
            ("zh-CN", "主人摸了摸你的头。", "主人，你说什么？"),
            ("en", "Master gently patted your head.", "What did you say, Master?"),
        ):
            with self.subTest(language=language):
                pet = self.make_pet(self.directory, language=language)
                body = pet._viewer_layout.body_rect
                head = QPointF(body.center().x(), body.y() + 12)
                pet.mousePressEvent(QMouseEvent(
                    QMouseEvent.MouseButtonPress, head, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
                ))
                with patch.object(pet, "start_thread") as start:
                    pet.mouseMoveEvent(QMouseEvent(
                        QMouseEvent.MouseMove, head + QPointF(60, 0), Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
                    ))
                    start.assert_called_once_with(head_text, role="system")
                pet.input_mode = True
                pet.input_buffer = ""
                with patch.object(pet, "show_text") as show:
                    pet.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
                    show.assert_called_once_with(empty_text)

    def test_startup_restore_and_missing_audio_do_not_move_model(self):
        pet = self.make_pet(self.directory)
        with patch.object(pet._viewerex, "_send") as send:
            # The application normally moves/records (0,0) before the
            # adapter's explicit placement hook. That must not lose bbox.
            pet.move(0, 0)
            pet.remember_window_position()
            pet.place_live2d_overlay()
            self.assertEqual(pet.pos(), pet._viewer_layout.geometry.topLeft())
            pet.move(5000, 5000)
            pet.settings.display.window_x = 5000
            pet.settings.display.window_y = 5000
            pet.place_live2d_overlay()
            self.assertEqual(pet.pos(), pet._viewer_layout.geometry.topLeft())
            send.assert_not_called()
        result = ConversationResult(
            reply=CharacterReply(sentences=[CharacterSentence(
                zh="下午好。", en="Good afternoon.", ja="こんにちは。", emotion="高兴",
            )]),
            audio_paths=[], user_text="测试", is_user_message=True,
        )
        pet._playback_result = result
        pet._playback_index = 0
        with patch.object(pet._viewerex, "set_emotion") as emotion:
            pet._play_next_sentence()
            emotion.assert_called_once_with("高兴")
        pet.show_text("下午好。", typing=False)
        self.assertIn("下午好。", pet.display_text)
        self.assertTrue(pet.playback_timer.isActive())
        image = pet.grab().toImage()
        self.assertLessEqual(image.pixelColor(pet._viewer_layout.body_rect.center()).alpha(), 1)
        dialogue = pet._dialogue_rect()
        self.assertTrue(any(
            image.pixelColor(x, y).alpha() > 100
            for y in range(dialogue.top(), dialogue.bottom() + 1)
            for x in range(dialogue.left(), dialogue.right() + 1)
        ))

    def test_middle_drag_keeps_overlay_with_model_and_blocks_without_calibration(self):
        pet = self.make_pet(self.directory)
        body = pet._viewer_layout.body_rect
        point = QPointF(body.center())
        origin = pet.pos()
        global_press = QPointF(origin) + point
        press = QMouseEvent(
            QMouseEvent.MouseButtonPress, point, global_press,
            Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier,
        )
        pet.mousePressEvent(press)
        self.assertIsNone(pet.drag_offset)
        self.assertEqual(pet.pos(), origin)
        pet._viewerex._position = (900, -350)
        with (
            patch.object(ViewerEXRenderer, "ready", new_callable=PropertyMock, return_value=True),
            patch.object(pet._viewerex, "_send", return_value=True) as send,
        ):
            pet.mousePressEvent(press)
            pet.mouseMoveEvent(QMouseEvent(
                QMouseEvent.MouseMove, point + QPointF(35, 20),
                global_press + QPointF(35, 20),
                Qt.NoButton, Qt.MiddleButton, Qt.NoModifier,
            ))
            self.assertEqual(pet.pos(), origin + QPoint(35, 20))
            send.assert_called_once_with(13400, {"id": 1, "posX": 935, "posY": -370})
            pet.mouseReleaseEvent(QMouseEvent(
                QMouseEvent.MouseButtonRelease, point, Qt.MiddleButton, Qt.NoButton, Qt.NoModifier,
            ))
            self.assertIsNone(pet.drag_offset)
            self.assertIsNone(pet._viewer_drag_origin)

    def test_visible_subtitle_drag_uses_one_desktop_anchor_and_preserves_input(self):
        pet = self.make_pet(self.directory, managed=True)
        pet.input_mode = True
        pet.input_buffer, pet.preedit_text = "你好，宁宁", "今天"
        pet.show_text("输入中的草稿", typing=False)
        original_text = pet.display_text
        pet.touch_head, pet.head_press_x = True, 10
        origin = pet.pos()
        text_origin = pet._dialogue_rect().translated(origin)
        body_origin = pet._viewer_layout.body_rect.translated(origin)
        global_press = origin + pet._dialogue_rect().center()
        self.assertFalse(body_origin.contains(global_press))
        with (
            patch.object(ViewerEXRenderer, "ready", new_callable=PropertyMock, return_value=True),
            patch.object(pet._viewerex, "_send", return_value=True) as send,
            patch.object(pet, "_cancel_active_jobs") as cancel,
            patch.object(pet, "start_thread") as start,
        ):
            pet.mousePressEvent(QMouseEvent(
                QMouseEvent.MouseButtonPress,
                QPointF(global_press - pet.pos()), QPointF(global_press),
                Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier,
            ))
            self.assertIsNotNone(pet.drag_offset)
            self.assertFalse(pet.touch_head)
            self.assertIsNone(pet.head_press_x)
            # Each event's local position changes with the moved window. The
            # model and subtitle must still get the total desktop displacement.
            for delta in (QPoint(35, 20), QPoint(80, -15), QPoint(-25, 30)):
                pointer = global_press + delta
                pet.mouseMoveEvent(QMouseEvent(
                    QMouseEvent.MouseMove,
                    QPointF(pointer - pet.pos()), QPointF(pointer),
                    Qt.NoButton, Qt.MiddleButton, Qt.NoModifier,
                ))
                self.assertEqual(pet.pos(), origin + delta)
                self.assertEqual(pet._dialogue_rect().translated(pet.pos()), text_origin.translated(delta))
                self.assertEqual(pet._viewer_layout.body_rect.translated(pet.pos()), body_origin.translated(delta))
                send.assert_called_with(13400, {
                    "id": 1, "posX": 900 + delta.x(), "posY": -350 - delta.y(),
                })
            self.assertEqual((pet.input_buffer, pet.preedit_text), ("你好，宁宁", "今天"))
            self.assertEqual(pet.display_text, original_text)
            self.assertTrue(pet.input_mode)
            cancel.assert_not_called()
            start.assert_not_called()
            pet.mouseReleaseEvent(QMouseEvent(
                QMouseEvent.MouseButtonRelease,
                QPointF(pointer - pet.pos()), QPointF(pointer),
                Qt.MiddleButton, Qt.NoButton, Qt.NoModifier,
            ))
            self.assertIsNone(pet.drag_offset)
            self.assertIsNone(pet._viewer_drag_origin)

    def test_subtitle_hit_surface_disappears_with_text_and_left_click_does_not_type(self):
        pet = self.make_pet(self.directory, managed=True)
        point = pet._dialogue_rect().topLeft() + QPoint(2, 2)
        self.assertFalse(pet._viewer_layout.body_rect.contains(point))
        pet.show_text("宁宁", typing=False)
        # Far above the rendered glyphs, in the transparent space between them.
        self.assertEqual(pet.grab().toImage().pixelColor(point).alpha(), 1)
        with patch.object(pet, "begin_text_input") as begin:
            pet.mousePressEvent(QMouseEvent(
                QMouseEvent.MouseButtonPress, QPointF(point),
                Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
            ))
            begin.assert_not_called()
            self.assertFalse(pet.touch_head)
        pet._clear_dialogue()
        self.assertEqual(pet.grab().toImage().pixelColor(point).alpha(), 0)
        with patch.object(pet._viewerex, "begin_drag") as begin:
            pet.mousePressEvent(QMouseEvent(
                QMouseEvent.MouseButtonPress, QPointF(point),
                Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier,
            ))
            begin.assert_not_called()
            self.assertIsNone(pet.drag_offset)

    def test_subtitle_stays_with_model_when_position_send_fails(self):
        pet = self.make_pet(self.directory, managed=True)
        pet.show_text("下午好。", typing=False)
        origin = pet.pos()
        global_press = origin + pet._dialogue_rect().center()
        with (
            patch.object(ViewerEXRenderer, "ready", new_callable=PropertyMock, return_value=True),
            patch.object(pet._viewerex, "_send", side_effect=[False, True]) as send,
        ):
            pet.mousePressEvent(QMouseEvent(
                QMouseEvent.MouseButtonPress,
                QPointF(global_press - origin), QPointF(global_press),
                Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier,
            ))
            for delta, expected in ((QPoint(30, 20), QPoint()), (QPoint(50, 40), QPoint(50, 40))):
                pointer = global_press + delta
                pet.mouseMoveEvent(QMouseEvent(
                    QMouseEvent.MouseMove,
                    QPointF(pointer - pet.pos()), QPointF(pointer),
                    Qt.NoButton, Qt.MiddleButton, Qt.NoModifier,
                ))
                self.assertEqual(pet.pos(), origin + expected)
                self.assertEqual(pet._viewerex._position, (900 + expected.x(), -350 - expected.y()))
            self.assertEqual(send.call_count, 2)

    def test_subtitle_does_not_move_alone_after_viewer_disconnect(self):
        pet = self.make_pet(self.directory, managed=True)
        pet.show_text("下午好。", typing=False)
        origin = pet.pos()
        global_press = origin + pet._dialogue_rect().center()
        with (
            patch.object(ViewerEXRenderer, "ready", new_callable=PropertyMock, return_value=True),
            patch.object(pet._viewerex, "_send", return_value=True) as send,
        ):
            pet.mousePressEvent(QMouseEvent(
                QMouseEvent.MouseButtonPress,
                QPointF(global_press - origin), QPointF(global_press),
                Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier,
            ))
            pet._viewerex._disconnected()
            # Reconnect only restores the last controlled model position; an
            # interrupted mouse drag cannot silently move Qt by itself.
            pet._viewerex._received('{"msg":10000,"msgId":9}')
            send.reset_mock()
            pointer = global_press + QPoint(35, 20)
            pet.mouseMoveEvent(QMouseEvent(
                QMouseEvent.MouseMove,
                QPointF(pointer - pet.pos()), QPointF(pointer),
                Qt.NoButton, Qt.MiddleButton, Qt.NoModifier,
            ))
            self.assertEqual(pet.pos(), origin)
            send.assert_not_called()
            pet.mouseReleaseEvent(QMouseEvent(
                QMouseEvent.MouseButtonRelease,
                QPointF(pointer - pet.pos()), QPointF(pointer),
                Qt.MiddleButton, Qt.NoButton, Qt.NoModifier,
            ))
            self.assertIsNone(pet.drag_offset)
            self.assertIsNone(pet._viewer_drag_origin)

    def test_png_middle_drag_retains_original_local_coordinate_behavior(self):
        pet = self.make_pet(self.directory)
        pet._viewerex.stop()
        pet._viewerex = None
        pet._viewer_layout = None
        origin = pet.pos()
        point = QPointF(100, 180)
        pet.mousePressEvent(QMouseEvent(
            QMouseEvent.MouseButtonPress, point, QPointF(3000, 4000),
            Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier,
        ))
        pet.mouseMoveEvent(QMouseEvent(
            QMouseEvent.MouseMove, point + QPointF(35, 20), QPointF(-6000, -7000),
            Qt.NoButton, Qt.MiddleButton, Qt.NoModifier,
        ))
        self.assertEqual(pet.pos(), origin + QPoint(35, 20))
        self.assertIsNone(pet._viewer_drag_origin)

    def test_managed_restart_aligns_overlay_with_canonical_model_position(self):
        pet = self.make_pet(self.directory, managed=True, saved_position=(20, 30))
        pet.move(20, 30)
        with patch.object(pet._viewerex, "_send") as send:
            pet.place_live2d_overlay()
            self.assertEqual(pet.pos(), pet._viewer_layout.geometry.topLeft())
            # Only the registration ACK moves the model, never Qt placement.
            send.assert_not_called()
            pet.move(150, 130)
            pet.remember_window_position()
            pet.place_live2d_overlay()
            self.assertEqual(pet.pos(), QPoint(150, 130))
            send.assert_not_called()

    def test_original_portrait_renderer_remains_available(self):
        with (
            patch.dict(os.environ, {"AIPET_RENDERER": "", "AIPET_CHARACTER_NAME": "", "AIPET_DATA_DIR": self.directory}),
            patch("aipet.ui.pet.generate_fgimage", return_value=np.zeros((800, 400, 4), dtype=np.uint8)) as generate,
        ):
            pet = Murasame(AppSettings(ui_language="zh-CN"))
            try:
                self.assertIsNone(pet._viewerex)
                self.assertEqual(pet.pet_name, "丛雨")
                self.assertEqual(pet.windowTitle(), "丛雨 · 桌宠")
                self.assertEqual(pet.accessibleName(), pet.windowTitle())
                generate.assert_called_once()
                self.assertIsNotNone(pet._source_portrait_pixmap)
                pet.show_text("原人物对白", typing=False)
                self.assertEqual(pet.display_text, "【丛雨】\n原人物对白")
                updated = pet.settings.model_copy(deep=True)
                updated.ui_language = "en"
                pet.apply_settings(updated)
                self.assertEqual(pet.windowTitle(), "Murasame · Desktop Pet")
                self.assertEqual(pet.accessibleName(), pet.windowTitle())
            finally:
                pet.shutdown()
                pet.close()


if __name__ == "__main__":
    unittest.main()
