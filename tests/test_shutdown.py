from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEventLoop, QObject, QThread, QTimer
from PyQt5.QtWidgets import QApplication

from aipet.application import _run_until_shutdown, configure_voice_trigger
from aipet.core.config import AppSettings
from aipet.core.download_manager import DownloadManager
from aipet.ui.pet import Murasame
from aipet.ui.settings_dialog import SettingsDialog
from aipet.ui.shutdown import (
    GracefulQuit, cancel_owned_threads, freeze_settings, threads_busy, voice_busy,
)


class BlockedWorker(QThread):
    """A short controlled stand-in for an in-flight, uncancellable HTTP call."""

    def __init__(self, parent):
        super().__init__(parent)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancelled = False

    def run(self):
        self.entered.set()
        self.release.wait(3)

    def cancel(self):
        self.cancelled = True
        self.requestInterruption()


class ShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(
            prefix=".test-shutdown-", dir=Path(__file__).resolve().parents[1],
        )))
        self.enterContext(patch.dict(os.environ, {
            "AIPET_DATA_DIR": str(self.root), "AIPET_RENDERER": "",
            "AIPET_CHARACTER_NAME": "",
        }))
        self.enterContext(patch("aipet.ui.pet.native_topmost_available", return_value=False))

    def wait_for(self, condition, timeout=2):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertTrue(condition())

    def worker(self, owner):
        worker = BlockedWorker(owner)
        worker.start()
        self.assertTrue(worker.entered.wait(.5))
        self.addCleanup(self.release_worker, worker)
        return worker

    @staticmethod
    def release_worker(worker):
        worker.release.set()
        try:
            worker.wait(1000)
        except RuntimeError:
            pass  # A completed worker's local QObject owner was disposed.

    def pet(self):
        settings = AppSettings(ui_language="zh-CN")
        settings.idle.do_not_disturb = True
        settings.tts.enabled = settings.stt.enabled = settings.vision.enabled = False
        with patch("aipet.ui.pet.generate_fgimage", return_value=np.zeros((800, 400, 4), dtype=np.uint8)):
            pet = Murasame(settings)
        # Keep the Qt owner alive until test workers have been released.
        self.addCleanup(pet.close)
        return pet

    def coordinator(self, **kwargs):
        coordinator = GracefulQuit(self.app, interval_ms=5, **kwargs)
        self.addCleanup(coordinator.deleteLater)
        return coordinator

    def test_short_wait_does_not_establish_that_cancelled_thread_has_stopped(self):
        owner = QObject()
        worker = self.worker(owner)
        worker.cancel()
        self.assertFalse(worker.wait(25))
        self.assertTrue(worker.isRunning())
        # Reproduce the old predicate safely, retaining its QObject parent.
        worker.release.set()
        self.assertTrue(worker.wait(1000))

    def test_request_hides_immediately_and_keeps_event_loop_alive_through_cleanup(self):
        pet = self.pet()
        worker = self.worker(pet)
        pet._workers[1] = worker
        pet.show()
        tray = Mock()
        cleanup_entered = threading.Event()
        cleanup_release = threading.Event()
        cleanup_on_ui_thread = []
        finalized, finished, beats = [], [], []

        def prepare():
            tray.hide()
            pet.begin_shutdown()
            pet.hide()
            cancel_owned_threads([pet])

        def cleanup():
            cleanup_on_ui_thread.append(QThread.currentThread() is self.app.thread())
            cleanup_entered.set()
            cleanup_release.wait(3)

        def finalize():
            pet.shutdown()
            finalized.append(True)

        coordinator = self.coordinator(
            prepare=prepare, busy=lambda: threads_busy([pet]),
            finalize_ui=finalize, cleanup=[cleanup], finish=lambda: finished.append(True),
        )
        heartbeat = QTimer()
        heartbeat.setInterval(3)
        heartbeat.timeout.connect(lambda: beats.append(1))
        heartbeat.start()
        try:
            with patch.object(worker, "terminate") as terminate:
                started = time.monotonic()
                coordinator.request()
                self.assertLess(time.monotonic() - started, .1)
                self.assertFalse(pet.isVisible())
                self.assertTrue(worker.cancelled)
                tray.hide.assert_called_once()
                self.wait_for(lambda: len(beats) >= 3)
                self.assertTrue(worker.isRunning())
                self.assertFalse(finalized)
                self.assertFalse(cleanup_entered.is_set())
                worker.release.set()
                self.wait_for(cleanup_entered.is_set)
                self.assertTrue(finalized)
                self.assertFalse(finished)
                beat_count = len(beats)
                self.wait_for(lambda: len(beats) >= beat_count + 3)
                self.assertEqual(cleanup_on_ui_thread, [False])
                cleanup_release.set()
                self.wait_for(lambda: coordinator.completed)
                self.assertEqual(finished, [True])
                coordinator.request()
                self.assertEqual(finished, [True])
                terminate.assert_not_called()
        finally:
            heartbeat.stop()
            worker.release.set()
            cleanup_release.set()
            self.wait_for(lambda: coordinator.completed)

    def test_settings_workers_and_queued_checks_are_drained_before_owner_disposal(self):
        with (
            patch.object(SettingsDialog, "_update_whisper_state"),
            patch.object(SettingsDialog, "_update_tts_state"),
            patch.object(SettingsDialog, "_refresh_audio_input_devices"),
            patch.object(SettingsDialog, "_auto_fetch_models"),
        ):
            dialog = SettingsDialog(AppSettings())
            dialog.show()
        self.addCleanup(dialog.deleteLater)
        attributes = (
            "_model_worker", "_whisper_check_worker", "_tts_check_worker",
            "_tts_service_worker", "_tts_bootstrap_worker",
        )
        workers = [self.worker(dialog) for _ in attributes]
        for attribute, worker in zip(attributes, workers):
            setattr(dialog, attribute, worker)
        dialog._model_fetch_queue = [True, False]
        dialog._pending_whisper_model = ("fake", "fake")
        dialog._pending_tts_check = True
        complete = []
        coordinator = self.coordinator(
            prepare=lambda: (freeze_settings(dialog), cancel_owned_threads([dialog])),
            busy=lambda: threads_busy([dialog]), finalize_ui=lambda: None,
            cleanup=[], finish=lambda: complete.append(True),
        )
        try:
            coordinator.request()
            self.assertTrue(dialog._closing)
            self.assertFalse(dialog.isVisible())
            self.assertFalse(dialog.isEnabled())
            self.assertEqual(dialog._model_fetch_queue, [])
            self.assertIsNone(dialog._pending_whisper_model)
            self.assertFalse(dialog._pending_tts_check)
            self.assertTrue(all(worker.cancelled for worker in workers))
            for worker in workers[:-1]:
                worker.release.set()
            self.wait_for(lambda: all(not worker.isRunning() for worker in workers[:-1]))
            self.assertFalse(complete)
            workers[-1].release.set()
            self.wait_for(lambda: coordinator.completed)
            self.assertEqual(complete, [True])
        finally:
            for worker in workers:
                worker.release.set()
            self.wait_for(lambda: coordinator.completed)
            for attribute in attributes:
                setattr(dialog, attribute, None)

    def test_speech_busy_delays_cache_cleanup_and_old_or_mock_triggers_are_compatible(self):
        class Voice:
            active = True
            def stop(self):
                pass  # An already-running recognition still has to return.
            def is_busy(self):
                return self.active

        voice = Voice()
        cleared = []
        coordinator = self.coordinator(
            prepare=voice.stop, busy=lambda: voice_busy(voice),
            finalize_ui=lambda: None, cleanup=[lambda: cleared.append(True)], finish=lambda: None,
        )
        coordinator.request()
        self.app.processEvents()
        self.assertFalse(cleared)
        voice.active = False
        self.wait_for(lambda: coordinator.completed)
        self.assertEqual(cleared, [True])
        self.assertFalse(voice_busy(object()))
        self.assertFalse(voice_busy(Mock()))

    def test_download_shutdown_runs_only_after_worker_has_joined(self):
        downloads = DownloadManager()
        worker = self.worker(downloads)
        downloads._workers["controlled-test"] = worker
        finalized = []
        def finalize():
            self.assertFalse(worker.isRunning())
            downloads.shutdown()
            finalized.append(True)
        coordinator = self.coordinator(
            prepare=lambda: cancel_owned_threads([downloads]),
            busy=lambda: threads_busy([downloads]),
            finalize_ui=finalize, cleanup=[], finish=lambda: None,
        )
        with patch.object(worker, "terminate") as terminate:
            coordinator.request()
            self.app.processEvents()
            self.assertFalse(finalized)
            worker.release.set()
            self.wait_for(lambda: coordinator.completed)
            self.assertEqual(finalized, [True])
            terminate.assert_not_called()

    def test_cleanup_failure_does_not_skip_other_owned_resource_cleanup(self):
        cleaned = []
        def failed_cleanup():
            raise OSError("controlled cleanup failure")
        coordinator = self.coordinator(
            prepare=lambda: None, busy=lambda: False, finalize_ui=lambda: None,
            cleanup=[failed_cleanup, lambda: cleaned.append(True)], finish=lambda: None,
        )
        coordinator.request()
        self.wait_for(lambda: coordinator.completed)
        self.assertEqual(cleaned, [True])

    def test_stop_failure_still_preserves_owners_until_busy_work_finishes(self):
        active = [True]
        def failed_stop():
            raise OSError("controlled device-close failure")
        coordinator = self.coordinator(
            prepare=failed_stop, busy=lambda: active[0], finalize_ui=lambda: None,
            cleanup=[], finish=lambda: None,
        )
        coordinator.request()
        self.app.processEvents()
        self.assertFalse(coordinator.completed)
        active[0] = False
        self.wait_for(lambda: coordinator.completed)

    def test_closing_pet_rejects_late_input_and_preserves_existing_history(self):
        pet = self.pet()
        pet.history = [{"role": "user", "content": "保留测试历史"}]
        pet.show_text("已有台词", typing=False)
        previous = pet.display_text
        pet.begin_shutdown()
        with (
            patch("aipet.ui.pet.ConversationWorker") as conversation,
            patch("aipet.ui.pet.VisionWorker") as vision,
            patch("aipet.ui.pet.get_idle_seconds") as idle,
        ):
            pet.start_thread("迟到语音", source="voice")
            pet._capture_screen()
            pet.check_idle_state()
            pet.show_text("迟到录音状态")
            pet.clear_history()
            conversation.assert_not_called()
            vision.assert_not_called()
            idle.assert_not_called()
        self.assertEqual(pet.display_text, previous)
        pet.shutdown()
        payload = json.loads((self.root / "history.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["messages"], pet.history)
        self.assertFalse(pet.typing_timer.isActive())

    def test_early_quit_resumes_qt_events_until_background_work_finishes(self):
        owner = QObject()
        worker = self.worker(owner)
        loop = QEventLoop()
        app_proxy = Mock()
        app_proxy.exec_.side_effect = loop.exec_
        prepared, done = [], []
        def finish():
            done.append(True)
            loop.quit()
        coordinator = self.coordinator(
            prepare=lambda: prepared.append(True), busy=lambda: threads_busy([owner]),
            finalize_ui=lambda: None, cleanup=[], finish=finish,
        )
        QTimer.singleShot(0, loop.quit)
        QTimer.singleShot(40, worker.release.set)
        self.assertEqual(_run_until_shutdown(app_proxy, coordinator), 0)
        self.assertTrue(coordinator.requested)
        self.assertEqual(prepared, [True])
        self.assertTrue(coordinator.completed)
        self.assertGreaterEqual(app_proxy.exec_.call_count, 2)
        self.assertEqual(done, [True])

    def test_direct_exit_return_resumes_events_until_drain_finishes(self):
        app, coordinator = Mock(), Mock()
        coordinator.completed = False
        calls = []
        def exec_loop():
            calls.append(1)
            if len(calls) == 2:
                coordinator.completed = True
            return 42 if len(calls) == 1 else 0
        app.exec_.side_effect = exec_loop
        self.assertEqual(_run_until_shutdown(app, coordinator), 42)
        coordinator.request.assert_called_once()
        self.assertEqual(len(calls), 2)

    def test_voice_trigger_receives_character_specific_vocabulary(self):
        pet = QObject()
        pet.start_thread, pet.show_text = Mock(), Mock()
        pet.is_dnd_enabled = Mock(return_value=False)
        settings = AppSettings()
        settings.stt.enabled = True
        settings.character.profile_id = "nene"
        runtime, tray = Mock(), Mock()
        trigger = configure_voice_trigger(pet, settings, tray, runtime)
        self.assertEqual(runtime.input.create_voice_trigger.call_args.kwargs["vocabulary_profile"], "nene")
        trigger._qt_bridge.text_ready.emit("测试语音", 0)
        pet.start_thread.assert_called_once_with("测试语音", role="user", source="voice")


if __name__ == "__main__":
    unittest.main()
