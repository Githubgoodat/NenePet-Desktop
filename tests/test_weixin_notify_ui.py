"""Synthetic QR/account/task fixtures; no real login or phone message is sent."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication, QLineEdit

from aipet.core.config import AppSettings
from aipet.core.workflow_events import TaskSnapshot
from aipet.core.workflow_input_notices import InputNotice
from aipet.core.weixin_notify import (
    DEFAULT_BASE_URL, WeixinConfig, WeixinCredentials, WeixinNotifyError,
    WeixinQRCode, WeixinQRResult, WeixinSendResult, WeixinUpdate,
)
from aipet.ui.weixin_notify import WeixinNotifyController, WeixinWorker, notice_text, qr_available, render_qr
from aipet.ui.workflow_assistant import WorkflowController


CREDS = WeixinCredentials("synthetic-bot-token", "fixture-bot", "fixture-owner", DEFAULT_BASE_URL)


class Pet(QObject):
    pause_changed = pyqtSignal(bool)
    test_mode_changed = pyqtSignal(bool)
    notification = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.settings = AppSettings()
        self.settings.character.profile_id = "nene"
        self._closing = self._paused = False
        self._test_mode = False

    def is_dnd_enabled(self):
        return self._paused

    def pause(self, value):
        self._paused = value
        self.pause_changed.emit(value)

    def is_test_mode(self):
        return self._test_mode


class FakeWorker(QObject):
    result_ready = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, operation, arguments=(), parent=None):
        super().__init__(parent)
        self.operation, self.arguments = operation, arguments
        self.start = Mock()
        self.cancel = Mock()
        self.deleteLater = Mock()

    def complete(self, value):
        # Deliberately emit even after cancel to test the UI's epoch boundary.
        self.result_ready.emit(value)
        self.finished.emit()

    def fail(self, code):
        self.error.emit(code)
        self.finished.emit()


class WeixinUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.pet = Pet()
        self.jobs = []
        self.controllers = []
        self.load = self.enterContext(patch("aipet.ui.weixin_notify.load_config", return_value=WeixinConfig()))
        self.save = self.enterContext(patch("aipet.ui.weixin_notify.save_config"))
        self.logout = self.enterContext(patch("aipet.ui.weixin_notify.logout"))
        self.qr_renderer = Mock(return_value=QPixmap(12, 12))
        self.qr_check = Mock(return_value=True)

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

    def make(self, *, enabled=False, config=None):
        if config is not None:
            self.load.return_value = config
        elif enabled:
            self.load.return_value = WeixinConfig(True, CREDS, "synthetic-context", "synthetic-cursor")
        controller = WeixinNotifyController(self.pet, config_path=Path.cwd() / "fixture-unwritten" / "weixin.json",
                                            worker_factory=self.factory, clock=lambda: self.now,
                                            qr_renderer=self.qr_renderer, qr_check=self.qr_check)
        self.controllers.append(controller)
        return controller

    def snapshot(self, *, task="fixture-task", status="needs_attention", source="codex", seconds=0):
        return TaskSnapshot(task, source, "private-project-name-must-not-leave", "FixtureEvent", status,
                            (self.now + timedelta(seconds=seconds)).isoformat(), "fixture-turn")

    def input_notice(self, *, identity="question-one", status="input_requested", seconds=0, source="codex"):
        return InputNotice(
            notice_id=hashlib.sha256(identity.encode()).hexdigest(), task_id="private-input-task-id",
            turn_id="private-turn-id", project="private-question-project", status=status, source=source,
            observed_at=(self.now + timedelta(seconds=seconds)).isoformat(),
        )

    def test_default_disabled_initialization_does_not_start_network_or_save(self):
        controller = self.make()
        controller.enqueue(self.snapshot())
        self.assertEqual(self.jobs, [])
        self.save.assert_not_called()
        self.qr_check.assert_not_called()
        self.assertFalse(controller.dialog.enabled.isChecked())
        self.assertFalse(controller.dialog.findChildren(QLineEdit))  # No arbitrary recipient or token entry.

    def test_missing_qr_dependency_is_reported_before_request(self):
        controller = self.make()
        self.qr_check.return_value = False
        controller.dialog.login.click()
        self.assertEqual(self.jobs, [])
        self.assertIn("requirements-weixin.txt", controller.status_text)

    def test_explicit_qr_owner_context_test_then_save_sequence(self):
        controller = self.make()
        controller.dialog.login.click()
        self.assertEqual(self.jobs[-1].operation, "qr")
        self.jobs[-1].complete(WeixinQRCode("synthetic-qr-key", "synthetic-qr-content"))
        self.qr_renderer.assert_called_once_with("synthetic-qr-content")
        self.assertTrue(controller._poll_timer.isActive())
        controller.check_login()
        self.assertEqual(self.jobs[-1].operation, "poll")
        self.jobs[-1].complete(WeixinQRResult("confirmed", DEFAULT_BASE_URL, CREDS))
        self.assertFalse(controller._poll_timer.isActive())
        self.assertIn("本人", controller.status_text)
        self.assertFalse(controller._enabled)
        self.save.assert_not_called()
        controller.dialog.context.click()
        self.assertEqual(self.jobs[-1].operation, "context")
        self.jobs[-1].complete(WeixinUpdate("synthetic-context", "synthetic-cursor"))
        controller.dialog.test.click()
        self.assertEqual(self.jobs[-1].operation, "send")
        self.assertEqual(self.jobs[-1].arguments[:2], (CREDS, "synthetic-context"))
        self.jobs[-1].complete(WeixinSendResult(True))
        self.assertIn("受理不等于送达", controller.status_text)
        self.assertFalse(controller._enabled)
        controller.dialog.enabled.setChecked(True)
        controller.dialog.save.click()
        self.assertTrue(controller._enabled)
        self.save.assert_called_once()
        self.assertEqual(self.save.call_args.args[1], WeixinConfig(True, CREDS, "synthetic-context", "synthetic-cursor"))

    def test_scan_poll_is_bounded_and_error_stops_automatic_retry(self):
        controller = self.make()
        controller.login()
        self.jobs[-1].complete(WeixinQRCode("fixture", "fixture"))
        controller.check_login()
        self.jobs[-1].fail("network_error")
        self.assertFalse(controller._poll_timer.isActive())
        before = len(self.jobs)
        self.now += timedelta(minutes=3)
        controller.check_login()
        self.assertEqual(len(self.jobs), before)
        self.assertIsNone(controller._qrcode)

    def test_context_missing_and_unverified_scan_do_not_enable_or_send(self):
        controller = self.make()
        controller.send_test()
        controller.dialog.enabled.setChecked(True)
        controller.save_settings()
        self.assertFalse(controller._enabled)
        self.assertEqual(self.jobs, [])
        self.save.assert_not_called()
        controller.login()
        self.jobs[-1].complete(WeixinQRCode("fixture", "fixture"))
        controller.check_login()
        self.jobs[-1].complete(WeixinQRResult("needs_verification", DEFAULT_BASE_URL))
        self.assertIsNone(controller._credentials)
        self.assertFalse(controller._poll_timer.isActive())

    def test_no_new_owner_context_is_not_fabricated(self):
        controller = self.make(config=WeixinConfig(False, CREDS))
        controller.establish_context()
        self.jobs[-1].complete(WeixinUpdate(None, "new-cursor"))
        self.assertEqual(controller._context, "")
        self.assertEqual(controller._cursor, "new-cursor")
        self.assertIn("尚未收到", controller.status_text)

    def test_minus_two_rejection_pauses_sends_without_erasing_binding_or_guessing_expiry(self):
        controller = self.make(enabled=True)
        controller.enqueue(self.snapshot())
        controller.enqueue(self.snapshot(task="pending-before-rejection"))
        self.assertTrue(controller._queue)
        controller._job_error("send", "api_rejected_minus2")
        self.assertFalse(controller._enabled)
        self.assertFalse(controller.dialog.enabled.isChecked())
        self.assertFalse(controller._queue)
        self.assertEqual(controller._credentials, CREDS)
        self.assertIn("-2", controller.status_text)
        self.assertNotIn("登录已过期", controller.status_text)
        controller.enqueue(self.snapshot(task="after-rejection"))
        self.assertEqual(len(self.jobs), 1)
        self.save.assert_not_called()

    def test_task_notice_contains_only_fixed_source_state_and_hash(self):
        record = self.snapshot(task="sensitive-id-do-not-send", source="grok_bot", status="phase_ended")
        text = notice_text(record)
        self.assertIn("协作上报", text)
        self.assertIn("不代表整个任务完成", text)
        self.assertNotIn(record.task_id, text)
        self.assertNotIn(record.project, text)
        self.assertNotIn(record.turn_id, text)
        controller = self.make(enabled=True)
        controller.enqueue(record)
        self.assertEqual(self.jobs[-1].arguments[-1], text)
        self.assertEqual(self.jobs[-1].arguments[0], CREDS)

    def test_unwanted_states_stale_or_unknown_source_do_not_send(self):
        controller = self.make(enabled=True)
        for record in (self.snapshot(status="running"), self.snapshot(source="unknown"),
                       self.snapshot(seconds=-601)):
            controller.enqueue(record)
        self.assertEqual(self.jobs, [])

    def test_same_task_state_rate_limit_and_four_waiting_notice_limit(self):
        controller = self.make(enabled=True)
        record = self.snapshot()
        controller.enqueue(record)
        controller.enqueue(record)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(controller._queue), 0)
        for index in range(5):
            controller.enqueue(self.snapshot(task="fixture-queued-" + str(index)))
        self.assertEqual(len(controller._queue), 4)
        self.assertIn("队列已满", controller.status_text)

    def test_dnd_clears_pending_and_ignores_late_response_without_replay(self):
        controller = self.make(enabled=True)
        controller.enqueue(self.snapshot())
        active = self.jobs[-1]
        controller.enqueue(self.snapshot(task="fixture-pending"))
        self.pet.pause(True)
        active.cancel.assert_called_once()
        self.assertFalse(controller._queue)
        active.complete(WeixinSendResult(True))
        self.assertIn("免打扰", controller.status_text)
        self.assertNotIn("已受理", controller.status_text)
        self.pet.pause(False)
        self.assertEqual(len(self.jobs), 1)
        self.assertIn("不补发", controller.status_text)

    def test_stop_tracking_and_test_mode_invalidate_in_flight_work(self):
        controller = self.make(enabled=True)
        controller.enqueue(self.snapshot())
        worker = self.jobs[-1]
        controller.set_tracking(False)
        worker.complete(WeixinSendResult(True))
        controller.set_tracking(True)
        self.assertEqual(len(self.jobs), 1)
        self.now += timedelta(seconds=31)
        controller.enqueue(self.snapshot())
        worker = self.jobs[-1]
        self.pet.test_mode_changed.emit(True)
        worker.complete(WeixinSendResult(True))
        self.assertNotIn("已受理", controller.status_text)

    def test_shutdown_ignores_late_qr_and_does_not_open_or_send(self):
        controller = self.make()
        controller.login()
        worker = self.jobs[-1]
        controller.shutdown()
        worker.complete(WeixinQRCode("late-key", "late-content"))
        controller.open()
        controller.send_test()
        controller.enqueue(self.snapshot())
        self.qr_renderer.assert_not_called()
        self.assertFalse(controller.dialog.isVisible())
        self.assertEqual(len(self.jobs), 1)

    def test_closing_settings_cancels_interactive_login_but_keeps_enabled_notice_channel(self):
        controller = self.make()
        controller.login()
        worker = self.jobs[-1]
        controller.dialog.close()
        worker.complete(WeixinQRCode("late", "late"))
        worker.cancel.assert_called_once()
        self.qr_renderer.assert_not_called()
        self.assertFalse(controller._poll_timer.isActive())
        self.save.assert_not_called()
        other = self.make(enabled=True)
        other.dialog.close()
        other.enqueue(self.snapshot())
        self.assertEqual(self.jobs[-1].operation, "send")

    def test_load_or_save_failure_keeps_notifications_disabled(self):
        self.load.side_effect = WeixinNotifyError("config_error")
        controller = self.make()
        self.assertFalse(controller._enabled)
        self.assertIn("无法读取或解密", controller.status_text)
        controller._credentials, controller._context = CREDS, "synthetic-context"
        controller.dialog.enabled.setChecked(True)
        self.save.side_effect = WeixinNotifyError("config_error")
        controller.save_settings()
        self.assertFalse(controller._enabled)
        self.assertFalse(controller.dialog.enabled.isChecked())
        self.assertEqual(self.jobs, [])

    def test_logout_clears_only_explicit_own_login_via_core(self):
        controller = self.make(enabled=True)
        controller.clear_login()
        self.logout.assert_called_once_with(controller.config_path)
        self.assertIsNone(controller._credentials)
        self.assertEqual(controller._context, "")
        self.assertFalse(controller._enabled)

    def test_real_qr_renderer_is_local_and_uses_no_external_image_service(self):
        if not qr_available():
            self.skipTest("Optional qrcode dependency is absent; fake-worker UI coverage still runs")
        with patch("requests.Session", side_effect=AssertionError("QR must be local")):
            pixmap = render_qr("https://example.invalid/synthetic-login-only")
        self.assertFalse(pixmap.isNull())
        self.assertLessEqual(pixmap.width(), 330)
        self.assertEqual(pixmap.width(), pixmap.height())

    def test_input_notice_text_is_fixed_and_async_does_not_claim_pause(self):
        for status in ("input_requested", "question_opened"):
            with self.subTest(status=status):
                record = self.input_notice(status=status)
                text = notice_text(record)
                self.assertIn("Codex", text)
                for private in (record.task_id, record.turn_id, record.project, record.notice_id):
                    self.assertNotIn(private, text)
                self.assertNotIn("暂停", text)
        self.assertIn("可能仍在继续", notice_text(self.input_notice(status="question_opened")))
        self.assertIsNone(notice_text(self.input_notice(source="grok_bot")))

    def test_distinct_questions_bypass_task_rate_limit_but_same_notice_never_replays(self):
        controller = self.make(enabled=True)
        self.now += timedelta(seconds=1)
        first = self.input_notice(identity="first")
        second = self.input_notice(identity="second")
        controller.enqueue(first)
        controller.enqueue(first)
        controller.enqueue(second)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(controller._queue), 1)
        self.jobs[0].complete(WeixinSendResult(True))
        self.assertEqual(len(self.jobs), 2)
        self.jobs[1].complete(WeixinSendResult(True))
        self.now += timedelta(seconds=31)
        controller.enqueue(first)
        controller.enqueue(second)
        self.assertEqual(len(self.jobs), 2)

    def test_input_notice_missing_identity_unknown_source_stale_or_future_do_not_send(self):
        controller = self.make(enabled=True)
        self.now += timedelta(seconds=1)
        for record in (
                self.snapshot(status="input_requested"),
                self.input_notice(source="grok_bot"),
                self.input_notice(identity="stale", seconds=-601),
                self.input_notice(identity="future", seconds=1)):
            controller.enqueue(record)
        self.assertFalse(self.jobs)

    def test_question_queue_remains_bounded_and_full_drop_is_not_replayed(self):
        controller = self.make(enabled=True)
        self.now += timedelta(seconds=1)
        records = [self.input_notice(identity=f"queued-{index}") for index in range(6)]
        for record in records:
            controller.enqueue(record)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(controller._queue), 4)
        self.assertIn("队列已满", controller.status_text)
        self.jobs[0].complete(WeixinSendResult(True))
        self.assertEqual(len(self.jobs), 2)
        queued = len(controller._queue)
        controller.enqueue(records[-1])
        self.assertEqual(len(controller._queue), queued)

    def test_ordinary_endings_reserve_one_slot_for_input_and_approval_goes_next(self):
        controller = self.make(enabled=True)
        controller.enqueue(self.snapshot(task="active-ending", status="turn_ended"))
        for index in range(5):
            controller.enqueue(self.snapshot(task=f"ordinary-{index}", status="phase_ended"))
        self.assertEqual(len(controller._queue), 3)
        self.now += timedelta(seconds=1)
        question = self.input_notice(identity="reserved-input")
        controller.enqueue(question)
        self.assertEqual(len(controller._queue), 4)
        self.jobs[0].complete(WeixinSendResult(True))
        self.assertEqual(self.jobs[1].arguments[-1], notice_text(question))
        approval = self.snapshot(task="reserved-approval", status="needs_attention")
        controller.enqueue(approval)
        self.assertEqual(len(controller._queue), 4)
        self.jobs[1].complete(WeixinSendResult(True))
        self.assertEqual(self.jobs[2].arguments[-1], notice_text(approval))

    def test_critical_requests_displace_ordinary_updates_and_keep_fifo_order(self):
        controller = self.make(enabled=True)
        controller.enqueue(self.snapshot(task="active-ending", status="turn_ended"))
        for index in range(3):
            controller.enqueue(self.snapshot(task=f"ordinary-{index}", status="turn_ended"))
        self.now += timedelta(seconds=1)
        first = self.input_notice(identity="first-priority")
        second = self.input_notice(identity="second-priority", status="question_opened")
        third = self.snapshot(task="third-priority", status="needs_attention")
        for record in (first, second, third):
            controller.enqueue(record)
            self.assertLessEqual(len(controller._queue), 4)
        for index, record in enumerate((first, second, third)):
            self.jobs[index].complete(WeixinSendResult(True))
            self.assertEqual(self.jobs[index + 1].arguments[-1], notice_text(record))

    def test_all_critical_pending_queue_is_bounded_and_new_drop_never_replays(self):
        controller = self.make(enabled=True)
        self.now += timedelta(seconds=1)
        records = [self.input_notice(identity=f"all-critical-{index}") for index in range(6)]
        for record in records:
            controller.enqueue(record)
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(controller._queue), 4)
        self.assertTrue(all(critical for critical, _ in controller._queue))
        self.jobs[0].complete(WeixinSendResult(True))
        queued = len(controller._queue)
        controller.enqueue(records[-1])
        self.assertEqual(len(controller._queue), queued)
        self.assertEqual(len(self.jobs), 2)

    def test_workflow_poll_burst_keeps_input_slot_and_sends_it_before_endings(self):
        phone = self.make(enabled=True)
        phone.enqueue(self.snapshot(task="already-sending", status="turn_ended"))
        for index in range(3):
            phone.enqueue(self.snapshot(task=f"already-pending-{index}", status="phase_ended"))
        lifecycle = Mock(return_value=())
        questions = Mock(return_value=())
        workflow = WorkflowController(self.pet, inbox=Path("synthetic-ui-inbox"),
                                      clock=lambda: self.now, loader=lifecycle, input_loader=questions)
        self.controllers.append(workflow)
        workflow.notification_ready.connect(phone.enqueue)
        self.now += timedelta(seconds=1)
        question = self.input_notice(identity="same-poll-question", status="question_opened")
        questions.return_value = (question,)
        lifecycle.return_value = tuple(self.snapshot(task=f"new-ending-{index}", status="turn_ended")
                                      for index in range(6))
        workflow.poll()
        self.assertEqual(len(phone._queue), 4)
        self.assertEqual(workflow.dialog.table.rowCount(), 6)
        self.jobs[0].complete(WeixinSendResult(True))
        self.assertEqual(self.jobs[1].arguments[-1], notice_text(question))
        self.assertIn("可能仍在继续", self.jobs[1].arguments[-1])

    def test_pause_tracking_and_test_mode_cut_off_question_replay_after_resume(self):
        controller = self.make(enabled=True)
        transitions = (
            (lambda: self.pet.pause(True), lambda: self.pet.pause(False)),
            (lambda: controller.set_tracking(False), lambda: controller.set_tracking(True)),
            (lambda: self.pet.test_mode_changed.emit(True), lambda: self.pet.test_mode_changed.emit(False)),
        )
        for index, (pause, resume) in enumerate(transitions):
            pause()
            self.now += timedelta(seconds=1)
            record = self.input_notice(identity=f"during-pause-{index}")
            controller.enqueue(record)
            resume()
            controller.enqueue(record)
        self.assertFalse(self.jobs)
        self.now += timedelta(seconds=1)
        controller.enqueue(self.input_notice(identity="fresh-after-resume"))
        self.assertEqual(len(self.jobs), 1)

    def test_input_dedup_memory_is_bounded_without_replaying_pruned_fresh_id(self):
        controller = self.make(enabled=True)
        first = None
        for index in range(270):
            self.now += timedelta(seconds=1)
            record = self.input_notice(identity=f"bounded-{index}")
            first = first or record
            controller.enqueue(record)
            self.jobs[-1].complete(WeixinSendResult(True))
        self.assertEqual(len(self.jobs), 270)
        self.assertLessEqual(len(controller._input_seen), 256)
        controller.enqueue(first)
        self.assertEqual(len(self.jobs), 270)

    def test_initial_test_mode_prevents_task_phone_jobs(self):
        self.pet._test_mode = True
        controller = self.make(enabled=True)
        self.now += timedelta(seconds=1)
        controller.enqueue(self.input_notice())
        controller.enqueue(self.snapshot())
        controller.send_test()
        self.assertFalse(self.jobs)


class WeixinWorkerTests(unittest.TestCase):
    def test_cancelled_worker_never_constructs_a_client(self):
        factory = Mock()
        worker = WeixinWorker("qr", client_factory=factory)
        worker.cancel()
        worker.run()
        factory.assert_not_called()

    def test_worker_calls_only_requested_method_and_closes_client(self):
        client = Mock()
        client.send_notice.return_value = WeixinSendResult(True)
        worker = WeixinWorker("send", (CREDS, "fixture-context", "fixture-text"), client_factory=lambda: client)
        values = []
        worker.result_ready.connect(values.append)
        worker.run()
        self.assertEqual(values, [WeixinSendResult(True)])
        client.send_notice.assert_called_once_with(CREDS, "fixture-context", "fixture-text")
        client.get_updates.assert_not_called()
        client.close.assert_called_once()
        self.assertEqual(worker.arguments, ())

    def test_worker_error_does_not_echo_exception_body_or_token(self):
        client = Mock()
        client.request_qr.side_effect = RuntimeError("secret-token-and-remote-body")
        worker = WeixinWorker("qr", client_factory=lambda: client)
        errors = []
        worker.error.connect(errors.append)
        worker.run()
        self.assertEqual(errors, ["bad_response"])
        self.assertNotIn("secret", str(errors))
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
