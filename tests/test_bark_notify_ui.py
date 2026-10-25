"""Only synthetic devices and fake workers; never contacts a phone service."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QEventLoop, QObject, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QLineEdit

from aipet.core.bark_notify import BarkConfig, BarkNotifyError, BarkSendResult
from aipet.core.config import AppSettings
from aipet.core.workflow_events import TaskSnapshot
from aipet.core.workflow_input_notices import InputNotice
from aipet.ui.bark_notify import BarkNotifyController, BarkWorker
from aipet.ui.phone_router import PhoneNotificationRouter
from aipet.ui.weixin_notify import WeixinNotifyController, WeixinWorker, notice_text
from aipet.ui.shutdown import GracefulQuit, owned_threads, threads_busy
from aipet.core.weixin_notify import (
    DEFAULT_BASE_URL, WeixinConfig, WeixinCredentials, WeixinSendResult,
)


KEY = "synthetic-device-key"
CREDS = WeixinCredentials("synthetic-bot-token", "fixture-bot", "fixture-owner", DEFAULT_BASE_URL)


class Pet(QObject):
    pause_changed = pyqtSignal(bool)
    test_mode_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.settings = AppSettings()
        self.settings.character.profile_id = "nene"
        self._closing = self._paused = self._test_mode = False

    def is_dnd_enabled(self):
        return self._paused

    def is_test_mode(self):
        return self._test_mode

    def pause(self, enabled):
        self._paused = enabled
        self.pause_changed.emit(enabled)


class FakeWorker(QObject):
    result_ready = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, operation, arguments=(), parent=None):
        super().__init__(parent)
        self.operation, self.arguments = operation, arguments
        self.start, self.cancel, self.deleteLater = Mock(), Mock(), Mock()

    def complete(self, value):
        self.result_ready.emit(value)
        self.finished.emit()

    def fail(self, code):
        self.error.emit(code)
        self.finished.emit()


class BarkUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        self.pet = Pet()
        self.jobs, self.controllers = [], []
        self.load = self.enterContext(patch("aipet.ui.bark_notify.load_config", return_value=BarkConfig()))
        self.save = self.enterContext(patch("aipet.ui.bark_notify.save_config"))
        self.logout = self.enterContext(patch("aipet.ui.bark_notify.logout"))

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
            controller.dialog.close()
            controller.dialog.deleteLater()
        self.app.processEvents()

    def factory(self, *args, **kwargs):
        worker = FakeWorker(*args, **kwargs)
        self.jobs.append(worker)
        return worker

    def make(self, *, enabled=False, config=None, worker_factory=None, parent=None):
        self.load.return_value = config if config is not None else BarkConfig(enabled, KEY if enabled else "")
        controller = BarkNotifyController(
            self.pet, parent=parent, config_path=Path.cwd() / "fixture-unwritten" / "workflow-bark.json",
            worker_factory=worker_factory or self.factory, clock=lambda: self.now,
        )
        self.controllers.append(controller)
        return controller

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)

    def snapshot(self, *, task="private-task", status="needs_attention", source="codex", seconds=0,
                 turn="private-turn"):
        return TaskSnapshot(task, source, "private-project", "FixtureEvent", status,
                            (self.now + timedelta(seconds=seconds)).isoformat(), turn)

    def question(self, identity="question-one", *, status="question_opened", seconds=0):
        return InputNotice(
            notice_id=hashlib.sha256(identity.encode()).hexdigest(), task_id="private-question-task",
            turn_id="private-question-turn", project="private-question-project", status=status,
            observed_at=(self.now + timedelta(seconds=seconds)).isoformat(),
        )

    def test_default_off_initialization_has_no_network_and_key_is_masked(self):
        controller = self.make()
        controller.enqueue(self.snapshot())
        controller.send_test()
        self.assertFalse(self.jobs)
        self.save.assert_not_called()
        self.assertFalse(controller.dialog.enabled.isChecked())
        self.assertEqual(controller.dialog.key.echoMode(), QLineEdit.Password)
        self.assertEqual(controller.dialog.key.text(), "")
        self.assertIn("第三方", controller.dialog.info.text())
        self.assertIn("允许通知", controller.dialog.info.text())

    def test_loaded_key_is_not_populated_or_echoed(self):
        controller = self.make(enabled=True)
        self.assertEqual(controller.dialog.key.text(), "")
        self.assertIn("已有绑定", controller.dialog.key.placeholderText())
        self.assertNotIn(KEY, controller.status_text)
        self.assertFalse(self.jobs)

    def test_save_official_url_locally_then_explicit_test_sends_only_once(self):
        controller = self.make()
        controller.dialog.key.setText("https://api.day.app/" + KEY + "/")
        controller.dialog.enabled.setChecked(True)
        controller.dialog.save.click()
        self.save.assert_called_once_with(controller.config_path, BarkConfig(True, KEY))
        self.assertTrue(controller._enabled)
        self.assertEqual(controller.dialog.key.text(), "")
        self.assertFalse(self.jobs)
        controller.dialog.test.click()
        controller.dialog.test.click()
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.jobs[0].operation, "send")
        self.assertEqual(self.jobs[0].arguments[0], KEY)
        self.assertIn("主动发送的测试", self.jobs[0].arguments[1])
        self.jobs[0].complete(BarkSendResult(True))
        self.assertEqual(len(self.jobs), 1)
        self.assertIn("受理不等于送达", controller.status_text)
        self.assertNotIn(KEY, controller.status_text)

    def test_saved_disabled_binding_can_be_explicitly_tested_without_enabling(self):
        controller = self.make(config=BarkConfig(False, KEY))
        controller.send_test()
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(controller._enabled)
        self.save.assert_not_called()

    def test_invalid_url_and_missing_key_do_not_save_or_send(self):
        controller = self.make()
        controller.dialog.enabled.setChecked(True)
        controller.save_settings()
        self.assertIn("设备密钥", controller.status_text)
        controller.dialog.key.setText("https://untrusted.invalid/" + KEY)
        controller.save_settings()
        self.save.assert_not_called()
        self.assertFalse(self.jobs)
        self.assertNotIn("untrusted", controller.status_text)

    def test_load_and_save_failure_keep_auto_disabled_without_echoing_exception(self):
        self.load.side_effect = BarkNotifyError("config_error")
        controller = self.make()
        self.assertFalse(controller._enabled)
        self.assertIn("无法读取", controller.status_text)
        controller.dialog.key.setText(KEY)
        controller.dialog.enabled.setChecked(True)
        self.save.side_effect = RuntimeError("PRIVATE DEVICE KEY")
        controller.save_settings()
        self.assertFalse(controller._enabled)
        self.assertFalse(controller.dialog.enabled.isChecked())
        self.assertNotIn("PRIVATE", controller.status_text)
        self.assertFalse(self.jobs)

    def test_clear_binding_calls_only_own_core_logout(self):
        controller = self.make(enabled=True)
        controller.clear_login()
        self.logout.assert_called_once_with(controller.config_path)
        self.assertEqual(controller._device_key, "")
        self.assertFalse(controller._enabled)
        self.assertFalse(self.jobs)

    def test_automatic_notice_contains_authorized_project_and_no_private_identifiers(self):
        controller = self.make(enabled=True)
        self.advance()
        record = self.snapshot(status="phase_ended", source="grok_bot")
        controller.enqueue(record)
        self.assertEqual(self.jobs[-1].arguments, (KEY, notice_text(record)))
        text = self.jobs[-1].arguments[-1]
        self.assertEqual(text.count(record.project), 1)
        for private in (record.task_id, record.turn_id, KEY):
            self.assertNotIn(private, text)

    def test_unknown_running_stale_future_and_prebaseline_notices_do_not_send(self):
        controller = self.make(enabled=True)
        before = self.snapshot()
        self.advance()
        for record in (before, self.snapshot(source="unknown"), self.snapshot(status="running"),
                       self.snapshot(seconds=-601), self.snapshot(seconds=1)):
            controller.enqueue(record)
        self.assertFalse(self.jobs)

    def test_terminal_identity_ignores_changed_time_but_new_turn_or_state_can_notify(self):
        controller = self.make(enabled=True)
        self.advance()
        record = self.snapshot(status="turn_ended")
        controller.enqueue(record)
        self.jobs[-1].complete(BarkSendResult(True))
        self.advance(31)
        controller.enqueue(record)
        self.assertEqual(len(self.jobs), 1)
        controller.enqueue(self.snapshot(status="turn_ended"))
        self.assertEqual(len(self.jobs), 1)
        controller.enqueue(self.snapshot(turn="another-turn", status="turn_ended"))
        self.jobs[-1].complete(BarkSendResult(True))
        controller.enqueue(self.snapshot(turn="another-turn", status="interrupted"))
        self.assertEqual(len(self.jobs), 3)

    def test_attention_can_repeat_when_a_new_observation_arrives(self):
        controller = self.make(enabled=True)
        self.advance()
        record = self.snapshot(status="needs_attention")
        controller.enqueue(record)
        self.jobs[-1].complete(BarkSendResult(True))
        self.advance()
        controller.enqueue(self.snapshot(status="needs_attention"))
        self.assertEqual(len(self.jobs), 2)

    def test_input_identity_is_independent_and_async_text_does_not_claim_pause(self):
        controller = self.make(enabled=True)
        self.advance()
        first, second = self.question("first"), self.question("second")
        controller.enqueue(first)
        controller.enqueue(first)
        controller.enqueue(second)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(controller._queue), 1)
        self.jobs[0].complete(BarkSendResult(True))
        self.assertEqual(len(self.jobs), 2)
        self.assertIn("可能仍在继续", self.jobs[-1].arguments[-1])
        self.assertNotIn("暂停", self.jobs[-1].arguments[-1])

    def test_critical_priority_reserves_slot_displaces_ordinary_and_keeps_fifo(self):
        controller = self.make(enabled=True)
        self.advance()
        controller.enqueue(self.snapshot(task="active", status="turn_ended"))
        for index in range(5):
            controller.enqueue(self.snapshot(task=f"ordinary-{index}", status="phase_ended"))
        self.assertEqual(len(controller._queue), 3)
        first, second = self.question("first"), self.question("second")
        approval = self.snapshot(task="approval")
        for record in (first, second, approval):
            controller.enqueue(record)
            self.assertLessEqual(len(controller._queue), 4)
        for index, record in enumerate((first, second, approval)):
            self.jobs[index].complete(BarkSendResult(True))
            self.assertEqual(self.jobs[index + 1].arguments[-1], notice_text(record))

    def test_all_critical_queue_stays_bounded_and_dropped_notice_never_replays(self):
        controller = self.make(enabled=True)
        self.advance()
        records = [self.question(f"critical-{index}") for index in range(6)]
        for record in records:
            controller.enqueue(record)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(controller._queue), 4)
        self.jobs[0].complete(BarkSendResult(True))
        pending = len(controller._queue)
        controller.enqueue(records[-1])
        self.assertEqual(len(controller._queue), pending)

    def test_dnd_cancels_queue_and_late_result_and_resumes_without_backlog(self):
        controller = self.make(enabled=True)
        self.advance()
        controller.enqueue(self.snapshot(task="active"))
        active = self.jobs[-1]
        controller.enqueue(self.snapshot(task="pending"))
        self.pet.pause(True)
        self.assertFalse(controller._queue)
        active.cancel.assert_called_once()
        active.complete(BarkSendResult(True))
        self.assertIn("免打扰", controller.status_text)
        old = self.snapshot(task="during-dnd")
        self.pet.pause(False)
        controller.enqueue(old)
        self.assertEqual(len(self.jobs), 1)
        self.advance()
        controller.enqueue(self.snapshot(task="after-dnd"))
        self.assertEqual(len(self.jobs), 2)

    def test_tracking_test_mode_and_cancel_ignore_late_errors_and_old_records(self):
        transitions = (
            (lambda c: c.set_tracking(False), lambda c: c.set_tracking(True)),
            (lambda c: c.set_test_mode(True), lambda c: c.set_test_mode(False)),
            (lambda c: c.cancel(), lambda c: None),
        )
        for index, (stop, resume) in enumerate(transitions):
            with self.subTest(index=index):
                controller = self.make(enabled=True)
                self.advance()
                record = self.snapshot(task=f"active-{index}")
                controller.enqueue(record)
                job = self.jobs[-1]
                stop(controller)
                job.fail("api_rejected")
                resume(controller)
                before = len(self.jobs)
                controller.enqueue(record)
                self.assertEqual(len(self.jobs), before)
                self.assertNotIn("明确拒绝", controller.status_text)

    def test_initial_test_mode_or_other_profile_prevents_network(self):
        self.pet._test_mode = True
        controller = self.make(enabled=True)
        self.advance()
        controller.enqueue(self.snapshot())
        controller.send_test()
        self.assertFalse(self.jobs)
        self.pet._test_mode = False
        self.pet.settings.character.profile_id = "murasame"
        other = self.make(enabled=True)
        other.send_test()
        self.assertFalse(self.jobs)

    def test_unknown_result_does_not_retry_and_explicit_rejection_pauses_runtime(self):
        controller = self.make(enabled=True)
        self.advance()
        controller.enqueue(self.snapshot())
        job = self.jobs[-1]
        job.fail("network_error")
        self.assertEqual(len(self.jobs), 1)
        self.assertTrue(controller._enabled)
        self.advance()
        controller.enqueue(self.snapshot(task="later"))
        self.jobs[-1].fail("api_rejected")
        self.assertFalse(controller._enabled)
        self.assertFalse(controller._queue)
        self.assertEqual(len(self.jobs), 2)

    def test_closing_test_cancels_it_but_closing_settings_keeps_auto_channel(self):
        controller = self.make(config=BarkConfig(False, KEY))
        controller.send_test()
        job = self.jobs[-1]
        controller.dialog.close()
        job.complete(BarkSendResult(True))
        job.cancel.assert_called_once()
        self.assertNotIn("已受理", controller.status_text)
        other = self.make(enabled=True)
        self.advance()
        other.enqueue(self.snapshot())
        active = self.jobs[-1]
        other.dialog.close()
        active.cancel.assert_not_called()

    def test_seen_memory_is_bounded_without_replaying_pruned_fresh_record(self):
        controller = self.make(enabled=True)
        first = None
        for index in range(270):
            self.advance()
            record = self.question(f"bounded-{index}")
            first = first or record
            controller.enqueue(record)
            self.jobs[-1].complete(BarkSendResult(True))
        self.assertEqual(len(self.jobs), 270)
        self.assertLessEqual(len(controller._seen), 256)
        controller.enqueue(first)
        self.assertEqual(len(self.jobs), 270)

    def test_sender_shutdown_returns_immediately_and_graceful_quit_drains_owned_threads(self):
        for channel in ("bark", "weixin"):
            with self.subTest(channel=channel):
                entered, release, send_ended = threading.Event(), threading.Event(), threading.Event()
                client = Mock()

                def delayed_send(*_):
                    entered.set()
                    # Only a GUI QTimer releases this request. A synchronous
                    # join in prepare() would stall the GUI and fail promptly.
                    release.wait(3)
                    send_ended.set()
                    return BarkSendResult(True) if channel == "bark" else WeixinSendResult(True)

                client.send_notice.side_effect = delayed_send
                workers = []

                def factory(operation, arguments, parent=None):
                    worker_class = BarkWorker if channel == "bark" else WeixinWorker
                    worker = worker_class(operation, arguments, parent, client_factory=lambda: client)
                    workers.append(worker)
                    return worker

                if channel == "bark":
                    controller = self.make(enabled=True, worker_factory=factory, parent=self.app)
                else:
                    with patch("aipet.ui.weixin_notify.load_config",
                               return_value=WeixinConfig(True, CREDS, "synthetic-context")):
                        controller = WeixinNotifyController(
                            self.pet, parent=self.app, config_path=Path.cwd() / "fixture-unwritten" / "weixin.json",
                            worker_factory=factory, clock=lambda: self.now,
                        )
                    self.controllers.append(controller)
                self.advance()
                controller.enqueue(self.snapshot(task="owned-" + channel))
                self.assertTrue(entered.wait(1))
                worker = workers[0]
                self.assertIs(worker.parent(), controller)
                self.assertIs(controller.parent(), self.app)
                self.assertIn(worker, owned_threads([self.app]))
                self.assertTrue(threads_busy([self.app]))
                loop, finished, finalized, beats = QEventLoop(), [], [], []

                def finish():
                    finished.append(send_ended.is_set())
                    loop.quit()

                coordinator = GracefulQuit(
                    self.app, prepare=controller.shutdown, busy=lambda: threads_busy([self.app]),
                    finalize_ui=lambda: finalized.append(True), cleanup=(), finish=finish, interval_ms=2,
                )
                heartbeat = QTimer()
                heartbeat.setInterval(2)
                heartbeat.timeout.connect(lambda: beats.append(True))
                guard = QTimer()
                guard.setSingleShot(True)
                guard.timeout.connect(loop.quit)
                try:
                    heartbeat.start()
                    QTimer.singleShot(25, release.set)
                    guard.start(1000)
                    started = time.monotonic()
                    coordinator.request()
                    self.assertLess(time.monotonic() - started, .1)
                    self.assertFalse(coordinator.completed)
                    self.assertFalse(finalized)
                    self.assertTrue(worker.isRunning())
                    self.assertIn(worker, owned_threads([self.app]))
                    loop.exec_()
                    self.assertTrue(coordinator.completed)
                    self.assertEqual(finished, [True])
                    self.assertEqual(finalized, [True])
                    self.assertTrue(beats)
                    self.assertFalse(threads_busy([self.app]))
                    self.assertNotIn("已受理", controller.status_text)
                    client.close.assert_called_once()
                finally:
                    heartbeat.stop()
                    guard.stop()
                    coordinator._timer.stop()
                    release.set()
                    try:
                        worker.wait(1000)
                    except RuntimeError:
                        pass  # A completed worker may already be deleteLater'd.
                    coordinator.deleteLater()
                    self.app.processEvents()

    def test_real_router_transfers_definitive_wechat_failure_to_bark_without_parallel_send(self):
        backup = self.make(enabled=True)
        primary_jobs = []

        def primary_factory(*args, **kwargs):
            worker = FakeWorker(*args, **kwargs)
            primary_jobs.append(worker)
            return worker

        with patch("aipet.ui.weixin_notify.load_config",
                   return_value=WeixinConfig(True, CREDS, "synthetic-context")):
            primary = WeixinNotifyController(
                self.pet, config_path=Path.cwd() / "fixture-unwritten" / "weixin.json",
                worker_factory=primary_factory, clock=lambda: self.now,
            )
        self.controllers.append(primary)
        router = PhoneNotificationRouter(primary, backup)
        self.advance()
        first = self.question("primary-rejection")
        second = self.snapshot(task="pending")
        router.enqueue(first)
        router.enqueue(second)
        self.assertEqual(len(primary_jobs), 1)
        self.assertFalse(self.jobs)
        primary_jobs[0].fail("api_rejected_minus2")
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(backup._queue), 1)
        self.assertEqual(self.jobs[0].arguments[-1], notice_text(first))
        self.jobs[0].complete(BarkSendResult(True))
        self.assertEqual(self.jobs[1].arguments[-1], notice_text(second))
        self.advance()
        third = self.snapshot(task="future-direct-backup")
        router.enqueue(third)
        self.assertEqual(len(primary_jobs), 1)
        self.assertEqual(len(backup._queue), 1)
        router.shutdown()


class BarkWorkerTests(unittest.TestCase):
    def test_cancelled_worker_never_constructs_client(self):
        factory = Mock()
        worker = BarkWorker("send", (KEY, "fixed"), client_factory=factory)
        worker.cancel()
        worker.run()
        factory.assert_not_called()

    def test_worker_only_calls_fixed_send_and_clears_arguments(self):
        client = Mock()
        client.send_notice.return_value = BarkSendResult(True)
        worker = BarkWorker("send", (KEY, "fixed metadata"), client_factory=lambda: client)
        values = []
        worker.result_ready.connect(values.append)
        worker.run()
        client.send_notice.assert_called_once_with(KEY, "fixed metadata")
        client.close.assert_called_once()
        self.assertEqual(values, [BarkSendResult(True)])
        self.assertEqual(worker.arguments, ())

    def test_worker_never_echoes_raw_exception_or_unknown_code(self):
        for error in (RuntimeError("SECRET DEVICE KEY"), BarkNotifyError("unknown-sensitive-code")):
            with self.subTest(error_type=type(error).__name__):
                client = Mock()
                client.send_notice.side_effect = error
                worker = BarkWorker("send", (KEY, "fixed"), client_factory=lambda: client)
                errors = []
                worker.error.connect(errors.append)
                worker.run()
                self.assertEqual(errors, ["bad_response"])
                self.assertNotIn("SECRET", str(errors))
                client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
