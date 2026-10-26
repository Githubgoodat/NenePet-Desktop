"""Dedicated question transport with synthetic binding and fake workers only."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication

from aipet.core.config import AppSettings
from aipet.core.weixin_notify import (
    DEFAULT_BASE_URL, WeixinConfig, WeixinCredentials, WeixinIncomingReply,
    WeixinQRCode, WeixinQRResult, WeixinReplyUpdate, WeixinSendResult, WeixinUpdate,
)
from aipet.core.workflow_events import TaskSnapshot
from aipet.ui.weixin_notify import WeixinNotifyController, WeixinWorker, notice_text


CREDS = WeixinCredentials("PRIVATE-TOKEN", "fixture-bot@im.bot", "fixture-owner@im.wechat", DEFAULT_BASE_URL)


class Pet(QObject):
    pause_changed = pyqtSignal(bool)
    test_mode_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.settings = AppSettings()
        self.settings.character.profile_id = "nene"
        self._paused = self._test_mode = self._closing = False

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
        # Emit after cancellation as well, testing the receiving epoch boundary.
        self.result_ready.emit(value)
        self.finished.emit()

    def fail(self, code):
        self.error.emit(code)
        self.finished.emit()


class QuestionTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        self.pet = Pet()
        self.jobs, self.controllers = [], []
        self.load = self.enterContext(patch("aipet.ui.weixin_notify.load_config"))
        self.save = self.enterContext(patch("aipet.ui.weixin_notify.save_config"))
        self.logout = self.enterContext(patch("aipet.ui.weixin_notify.logout"))

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

    def make(self, *, saved_enabled=True, credentials=CREDS, context="PRIVATE-CONTEXT", ready=True):
        self.load.return_value = WeixinConfig(saved_enabled, credentials, context, "saved-cursor")
        controller = WeixinNotifyController(
            self.pet, config_path=Path.cwd() / "fixture-unwritten" / "weixin-question.json",
            worker_factory=self.factory, clock=lambda: self.now,
            qr_check=lambda: True, qr_renderer=lambda _: QPixmap(8, 8),
        )
        self.assertFalse(controller._bootstrap_ready)
        # Most cases exercise an established listener. Cold-start readiness is
        # tested separately through real callback flow below.
        controller._bootstrap_ready = ready
        self.controllers.append(controller)
        return controller

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)

    def update(self, *, context="NEW-CONTEXT", cursor="next-cursor"):
        message = WeixinIncomingReply("9007199254740993", "回复 deadbeef 普通答复", 1760000000123)
        return WeixinReplyUpdate(context, cursor, (message,))

    def notice(self, task="fixture-task"):
        return TaskSnapshot(task, "codex", "fixture-project", "Stop", "turn_ended",
                            self.now.isoformat(), "fixture-turn")

    def test_disabled_question_mode_has_no_poll_or_question_send(self):
        controller = self.make(saved_enabled=False)
        controller.poll_question_replies()
        self.assertFalse(controller.send_question_notice("PRIVATE-QUESTION"))
        self.assertFalse(controller.can_send_question())
        self.assertFalse(controller._question_timer.isActive())
        self.assertFalse(self.jobs)
        self.save.assert_not_called()

    def test_owner_key_binds_bot_and_scanner_without_exposing_identity(self):
        controller = self.make()
        owner = controller.question_owner_key()
        self.assertEqual(owner, hashlib.sha256((CREDS.bot_id + "\0" + CREDS.scanner_id).encode()).hexdigest())
        self.assertEqual(len(owner), 64)
        self.assertNotIn(CREDS.bot_id, owner)
        controller._credentials = replace(CREDS, bot_id="different-bot@im.bot")
        self.assertNotEqual(controller.question_owner_key(), owner)
        controller._credentials = replace(CREDS, scanner_id="different-owner@im.wechat")
        self.assertNotEqual(controller.question_owner_key(), owner)
        controller._credentials = None
        self.assertEqual(controller.question_owner_key(), "")
        self.assertFalse(controller.question_channel_available())

    def test_question_mode_has_one_cursor_consumer_and_manual_refresh_uses_replies(self):
        controller = self.make(saved_enabled=False)
        received = []
        slots = []
        controller.question_replies.connect(lambda messages, owner: received.append((messages, owner)))
        controller.slot_available.connect(lambda: slots.append(True))
        controller.set_question_channel(True)
        controller.poll_question_replies()
        controller.poll_question_replies()
        controller.establish_context()
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.jobs[0].operation, "replies")
        self.assertEqual(self.jobs[0].arguments, (CREDS, "saved-cursor"))
        update = self.update()
        self.jobs[0].complete(update)
        self.assertEqual(received, [(update.messages, controller.question_owner_key())])
        self.assertEqual(controller._context, "NEW-CONTEXT")
        self.assertEqual(controller._cursor, "next-cursor")
        self.save.assert_called_once_with(
            controller.config_path, WeixinConfig(False, CREDS, "NEW-CONTEXT", "next-cursor"),
        )
        self.assertFalse(controller._saved_enabled)
        self.assertFalse(controller._enabled)
        self.assertEqual(slots, [True])

    def test_unchanged_poll_does_not_rewrite_config_and_runtime_pause_preserves_saved_preference(self):
        controller = self.make(saved_enabled=True)
        controller._enabled = False
        controller.set_question_channel(True)
        controller.poll_question_replies()
        self.jobs[-1].complete(WeixinReplyUpdate(None, "saved-cursor", ()))
        self.save.assert_not_called()
        controller.poll_question_replies()
        self.jobs[-1].complete(self.update())
        self.assertTrue(self.save.call_args.args[1].enabled)
        self.assertFalse(controller._enabled)
        self.assertTrue(controller._saved_enabled)

    def test_manual_context_without_question_mode_keeps_context_only_path(self):
        controller = self.make()
        received = []
        controller.question_replies.connect(lambda *args: received.append(args))
        controller.establish_context()
        self.assertEqual(self.jobs[-1].operation, "context")
        self.jobs[-1].complete(WeixinUpdate("REFRESHED-CONTEXT", "refreshed-cursor"))
        self.assertEqual(controller._context, "REFRESHED-CONTEXT")
        self.assertEqual(controller._cursor, "refreshed-cursor")
        self.assertFalse(received)

    def test_one_receive_slot_can_run_in_parallel_with_independent_outgoing_send(self):
        controller = self.make()
        controller.set_question_channel(True)
        controller.enqueue(self.notice("first"))
        controller.poll_question_replies()
        controller.establish_context()
        self.assertEqual([job.operation for job in self.jobs], ["send", "replies"])
        outgoing, incoming = self.jobs
        self.assertIs(controller._active, outgoing)
        self.assertIs(controller._reply_active, incoming)
        controller.enqueue(self.notice("second"))
        controller.poll_question_replies()
        self.assertEqual(len(self.jobs), 2)
        incoming.complete(self.update())
        self.assertIs(controller._active, outgoing)
        self.assertIsNone(controller._reply_active)
        outgoing.complete(WeixinSendResult(True))
        self.assertEqual(self.jobs[-1].operation, "send")
        self.assertEqual(self.jobs[-1].arguments[1], "NEW-CONTEXT")
        self.assertEqual(self.jobs[-1].arguments[-1], notice_text(self.notice("second")))

    def test_disabling_question_mode_cancels_late_reply_batch_and_question_send_receipt(self):
        for operation in ("replies", "question_send"):
            with self.subTest(operation=operation):
                controller = self.make()
                received, sent, errors = [], [], []
                controller.question_replies.connect(lambda *value: received.append(value))
                controller.question_send_result.connect(sent.append)
                controller.question_transport_error.connect(errors.append)
                controller.set_question_channel(True)
                if operation == "replies":
                    controller.poll_question_replies()
                else:
                    self.assertTrue(controller.send_question_notice("PRIVATE-QUESTION"))
                worker = self.jobs[-1]
                before = self.save.call_count
                controller.set_question_channel(False)
                worker.cancel.assert_called_once()
                worker.complete(self.update() if operation == "replies" else WeixinSendResult(True))
                self.assertFalse(received)
                self.assertFalse(sent)
                self.assertFalse(errors)
                self.assertEqual(self.save.call_count, before)
                self.assertFalse(controller._question_timer.isActive())

    def test_disabling_questions_does_not_cancel_a_normal_notification(self):
        controller = self.make()
        controller.enqueue(self.notice())
        worker = self.jobs[-1]
        controller.set_question_channel(True)
        controller.set_question_channel(False)
        worker.cancel.assert_not_called()
        worker.complete(WeixinSendResult(True))
        self.assertIn("已受理", controller.status_text)

    def test_repeated_true_mode_does_not_invalidate_an_in_flight_question(self):
        controller = self.make()
        received = []
        controller.question_replies.connect(lambda *args: received.append(args))
        controller.set_question_channel(True)
        controller.poll_question_replies()
        worker = self.jobs[-1]
        controller.set_question_channel(True)
        controller.set_question_channel(True)
        worker.complete(self.update())
        self.assertEqual(len(received), 1)
        worker.cancel.assert_not_called()

    def test_dnd_test_and_tracking_suspend_late_data_and_restart_read_timer_on_resume(self):
        transitions = (
            (lambda c: c.set_paused(True), lambda c: c.set_paused(False)),
            (lambda c: c.set_test_mode(True), lambda c: c.set_test_mode(False)),
            (lambda c: c.set_tracking(False), lambda c: c.set_tracking(True)),
        )
        for stop, resume in transitions:
            controller = self.make()
            received = []
            controller.question_replies.connect(lambda *args: received.append(args))
            controller.set_question_channel(True)
            controller.poll_question_replies()
            old = self.jobs[-1]
            stop(controller)
            self.assertFalse(controller._question_timer.isActive())
            old.complete(self.update(cursor="must-not-advance"))
            self.assertFalse(received)
            self.assertEqual(controller._cursor, "saved-cursor")
            resume(controller)
            self.assertTrue(controller._question_timer.isActive())
            controller.poll_question_replies()
            self.jobs[-1].complete(self.update(cursor="fresh-after-resume"))
            self.assertEqual(len(received), 1)

    def test_shutdown_rejects_late_receive_and_send_results_or_errors(self):
        for operation in ("replies", "question_send"):
            with self.subTest(operation=operation):
                controller = self.make()
                received, sent, errors = [], [], []
                controller.question_replies.connect(lambda *args: received.append(args))
                controller.question_send_result.connect(sent.append)
                controller.question_transport_error.connect(errors.append)
                controller.set_question_channel(True)
                if operation == "replies":
                    controller.poll_question_replies()
                else:
                    controller.send_question_notice("PRIVATE-QUESTION")
                worker = self.jobs[-1]
                controller.shutdown()
                worker.complete(self.update() if operation == "replies" else WeixinSendResult(True))
                worker.fail("api_rejected_minus2")
                controller.poll_question_replies()
                self.assertFalse(received or sent or errors)
                self.assertFalse(controller._question_timer.isActive())

    def test_read_timeout_allows_only_read_poll_after_three_seconds(self):
        controller = self.make()
        errors = []
        controller.question_transport_error.connect(errors.append)
        controller.set_question_channel(True)
        controller.poll_question_replies()
        self.jobs[-1].fail("network_error")
        self.assertEqual(errors, ["network_error"])
        self.assertFalse(controller._question_fault)
        self.assertTrue(controller._question_timer.isActive())
        controller.poll_question_replies()
        self.assertEqual(len(self.jobs), 1)
        self.advance(2)
        controller.poll_question_replies()
        self.assertEqual(len(self.jobs), 1)
        self.advance()
        controller.poll_question_replies()
        self.assertEqual([job.operation for job in self.jobs], ["replies", "replies"])

    def test_hard_receive_errors_pause_until_explicit_retry(self):
        for code in ("api_rejected_minus2", "api_rejected", "session_expired", "bad_response"):
            with self.subTest(code=code):
                controller = self.make()
                errors = []
                controller.question_transport_error.connect(errors.append)
                controller.set_question_channel(True)
                controller.poll_question_replies()
                self.jobs[-1].fail(code)
                count = len(self.jobs)
                self.assertTrue(controller._question_fault)
                self.assertFalse(controller._question_timer.isActive())
                self.advance(10)
                controller.poll_question_replies()
                self.assertEqual(len(self.jobs), count)
                controller.retry_question_channel()
                self.assertFalse(controller.question_channel_available())
                self.assertFalse(controller.can_send_question())
                controller.poll_question_replies()
                self.assertEqual(len(self.jobs), count + 1)
                self.assertEqual(self.jobs[-1].operation, "replies")
                self.assertEqual(errors, [code])

    def test_question_send_reports_result_once_and_never_enters_automatic_fallback(self):
        for code in ("acceptance_unknown", "network_error", "bad_response", "api_rejected_minus2"):
            with self.subTest(code=code):
                before = len(self.jobs)
                controller = self.make()
                results, fallback = [], []
                controller.question_send_result.connect(results.append)
                controller.automatic_notice_failed.connect(fallback.append)
                controller.set_question_channel(True)
                self.assertTrue(controller.can_send_question())
                self.assertTrue(controller.send_question_notice("PRIVATE-QUESTION"))
                worker = self.jobs[-1]
                self.assertEqual(worker.operation, "question_send")
                self.assertIsNone(controller._active_auto_notice)
                self.assertFalse(controller.send_question_notice("PRIVATE-QUESTION"))
                count = len(self.jobs)
                worker.fail(code)
                self.assertEqual(results, [code])
                self.assertFalse(fallback)
                self.assertEqual(len(self.jobs), count)
                controller.poll_question_replies()
                self.assertEqual(self.jobs[-1].operation, "replies")
                self.assertFalse(controller._question_fault)
                self.assertEqual(sum(job.operation == "question_send" for job in self.jobs[before:]), 1)

    def test_question_acceptance_and_missing_context_have_explicit_semantics(self):
        controller = self.make(saved_enabled=False)
        results = []
        controller.question_send_result.connect(results.append)
        controller.set_question_channel(True)
        self.assertFalse(controller.can_send_automatic())
        self.assertTrue(controller.send_question_notice("PRIVATE-QUESTION"))
        self.jobs[-1].complete(WeixinSendResult(True))
        self.assertEqual(results, ["accepted"])
        no_context = self.make(context="")
        no_context.set_question_channel(True)
        self.assertFalse(no_context.can_send_question())
        no_context.poll_question_replies()
        self.assertEqual(self.jobs[-1].operation, "replies")
        self.jobs[-1].complete(self.update())
        self.assertTrue(no_context.can_send_question())

    def test_unconfirmed_question_result_is_reported_once_without_send_replay(self):
        controller = self.make()
        results, fallback = [], []
        controller.question_send_result.connect(results.append)
        controller.automatic_notice_failed.connect(fallback.append)
        controller.set_question_channel(True)
        self.assertTrue(controller.send_question_notice("PRIVATE-QUESTION"))
        self.jobs[-1].complete(WeixinSendResult(False))
        self.assertEqual(results, ["acceptance_unknown"])
        self.assertEqual(len(self.jobs), 1)
        self.assertFalse(fallback)

    def test_rejected_question_only_hands_off_queued_metadata_never_question_text(self):
        controller = self.make()
        results, fallback = [], []
        controller.question_send_result.connect(results.append)
        controller.automatic_notice_failed.connect(fallback.append)
        controller.set_question_channel(True)
        controller.send_question_notice("PRIVATE-QUESTION")
        worker = self.jobs[-1]
        queued = self.notice("queued-metadata")
        controller.enqueue(queued)
        worker.fail("api_rejected_minus2")
        self.assertEqual(results, ["api_rejected_minus2"])
        self.assertEqual(fallback, [queued])
        self.assertNotIn("PRIVATE-QUESTION", notice_text(fallback[0]))
        self.assertFalse(controller._queue)

    def test_normal_automatic_failure_keeps_its_metadata_fallback_behavior(self):
        controller = self.make()
        fallback = []
        controller.automatic_notice_failed.connect(fallback.append)
        controller.set_question_channel(True)
        record = self.notice()
        controller.enqueue(record)
        self.jobs[-1].fail("api_rejected_minus2")
        self.assertEqual(fallback, [record])
        self.assertFalse(controller.can_send_automatic())
        self.assertNotIn("PRIVATE-QUESTION", str(fallback))

    def test_cursor_save_failure_emits_fixed_error_and_pauses_receiving(self):
        controller = self.make()
        errors = []
        controller.question_transport_error.connect(errors.append)
        controller.set_question_channel(True)
        controller.poll_question_replies()
        self.save.side_effect = RuntimeError("PRIVATE-TOKEN PRIVATE-ANSWER")
        self.jobs[-1].complete(self.update())
        self.assertEqual(errors, ["config_error"])
        self.assertTrue(controller._question_fault)
        self.assertFalse(controller._question_timer.isActive())
        self.assertNotIn("PRIVATE", str(errors))

    def test_busy_broker_retains_cursor_but_saves_context_and_repolls_read_only(self):
        controller = self.make(saved_enabled=False)
        batches = []

        def receive(messages, owner):
            batches.append((messages, owner))
            if len(batches) == 1:
                controller.defer_question_update()

        controller.question_replies.connect(receive)
        controller.set_question_channel(True)
        controller.poll_question_replies()
        update = self.update(context="FRESH-OWNER-CONTEXT", cursor="uncommitted-cursor")
        self.jobs[-1].complete(update)
        self.assertEqual(controller._cursor, "saved-cursor")
        self.assertEqual(controller._context, "FRESH-OWNER-CONTEXT")
        self.assertEqual(self.save.call_args.args[1],
                         WeixinConfig(False, CREDS, "FRESH-OWNER-CONTEXT", "saved-cursor"))
        controller.poll_question_replies()
        self.assertEqual(len(self.jobs), 1)
        self.advance()
        controller.poll_question_replies()
        self.assertEqual(self.jobs[-1].arguments, (CREDS, "saved-cursor"))
        self.jobs[-1].complete(update)
        self.assertEqual(controller._cursor, "uncommitted-cursor")
        self.assertEqual(self.save.call_args.args[1],
                         WeixinConfig(False, CREDS, "FRESH-OWNER-CONTEXT", "uncommitted-cursor"))
        self.assertEqual([batch[0] for batch in batches], [update.messages, update.messages])
        self.assertEqual([job.operation for job in self.jobs], ["replies", "replies"])
        self.assertFalse(controller._saved_enabled)

    def test_new_qr_login_and_poll_keep_question_channel_unavailable_on_old_binding(self):
        controller = self.make()
        controller.set_question_channel(True)
        self.assertTrue(controller.question_channel_available())
        controller.login()
        self.assertEqual(self.jobs[-1].operation, "qr")
        self.assertFalse(controller.question_channel_available())
        self.assertFalse(controller.send_question_notice("MUST-NOT-USE-OLD-BINDING"))
        self.jobs[-1].complete(WeixinQRCode("fresh-qr", "synthetic-local-qr"))
        self.assertFalse(controller.question_channel_available())
        controller.poll_question_replies()
        self.assertEqual(len(self.jobs), 1)
        controller.check_login()
        self.assertEqual(self.jobs[-1].operation, "poll")
        self.assertFalse(controller.question_channel_available())
        self.jobs[-1].complete(WeixinQRResult("pending", DEFAULT_BASE_URL))
        self.assertFalse(controller.question_channel_available())

    def test_cold_start_requires_read_bootstrap_and_then_polling_does_not_block_new_questions(self):
        controller = self.make(ready=False)
        controller.set_question_channel(True)
        self.assertFalse(controller.question_channel_available())
        self.assertFalse(controller.can_send_question())
        controller.poll_question_replies()
        first_read = self.jobs[-1]
        first_read.complete(WeixinReplyUpdate(None, "saved-cursor", ()))
        self.assertTrue(controller._bootstrap_ready)
        self.assertTrue(controller.question_channel_available())
        controller.poll_question_replies()
        second_read = self.jobs[-1]
        self.assertTrue(controller.send_question_notice("PRIVATE-FIRST-QUESTION"))
        outgoing = self.jobs[-1]
        self.assertIs(controller._reply_active, second_read)
        self.assertIs(controller._active, outgoing)
        outgoing.complete(WeixinSendResult(True))
        self.assertIs(controller._reply_active, second_read)
        self.assertTrue(controller.send_question_notice("PRIVATE-SECOND-QUESTION"))
        self.assertEqual([job.operation for job in self.jobs],
                         ["replies", "replies", "question_send", "question_send"])

    def test_cold_start_without_context_receives_it_automatically_before_first_send(self):
        controller = self.make(context="", saved_enabled=False, ready=False)
        controller.set_question_channel(True)
        controller.poll_question_replies()
        self.jobs[-1].complete(WeixinReplyUpdate("FRESH-OWNER-CONTEXT", "cursor", ()))
        self.assertTrue(controller.question_channel_available())
        self.assertTrue(controller.send_question_notice("PRIVATE-QUESTION"))
        self.assertEqual(self.jobs[-1].arguments[1], "FRESH-OWNER-CONTEXT")
        self.assertFalse(controller._saved_enabled)

    def test_canceled_receive_must_physically_retire_before_a_new_receive_starts(self):
        controller = self.make()
        controller.set_question_channel(True)
        received = []
        controller.question_replies.connect(lambda *args: received.append(args))
        controller.poll_question_replies()
        old = self.jobs[-1]
        controller.set_question_channel(False)
        controller.set_question_channel(True)
        controller.poll_question_replies()
        self.assertEqual(len(self.jobs), 1)
        self.assertIs(controller._reply_active, old)
        old.complete(self.update(cursor="stale-cursor"))
        self.assertFalse(received)
        self.assertEqual(controller._cursor, "saved-cursor")
        controller.poll_question_replies()
        self.assertEqual(len(self.jobs), 2)
        self.assertIs(controller._reply_active, self.jobs[-1])

    def test_manual_refresh_waits_for_disabled_receive_to_retire_before_legacy_context(self):
        controller = self.make()
        controller.set_question_channel(True)
        controller.poll_question_replies()
        old = self.jobs[-1]
        controller.set_question_channel(False)
        controller.establish_context()
        self.assertEqual(len(self.jobs), 1)
        old.complete(self.update(cursor="ignored-after-cancel"))
        self.assertEqual([job.operation for job in self.jobs], ["replies", "context"])
        self.assertIsNone(controller._reply_active)
        self.jobs[-1].complete(WeixinUpdate("MANUAL-CONTEXT", "manual-cursor"))
        self.assertEqual(controller._context, "MANUAL-CONTEXT")
        self.assertEqual(controller._cursor, "manual-cursor")
        self.assertFalse(controller._bootstrap_ready)

    def test_legacy_context_read_finishes_before_enabling_the_reply_consumer(self):
        controller = self.make(ready=False)
        controller.establish_context()
        legacy = self.jobs[-1]
        controller.set_question_channel(True)
        controller.poll_question_replies()
        self.assertEqual([job.operation for job in self.jobs], ["context"])
        legacy.complete(WeixinUpdate("MANUAL-CONTEXT", "manual-cursor"))
        self.assertFalse(controller.question_channel_available())
        controller.poll_question_replies()
        self.assertEqual(self.jobs[-1].operation, "replies")
        self.assertEqual(self.jobs[-1].arguments, (CREDS, "manual-cursor"))
        self.jobs[-1].complete(WeixinReplyUpdate(None, "manual-cursor", ()))
        self.assertTrue(controller.question_channel_available())

    def test_send_rejection_does_not_invalidate_concurrent_owner_receive(self):
        for operation in ("send", "question_send"):
            with self.subTest(operation=operation):
                controller = self.make()
                received = []
                controller.question_replies.connect(lambda *args: received.append(args))
                controller.set_question_channel(True)
                controller.poll_question_replies()
                incoming = self.jobs[-1]
                if operation == "send":
                    controller.enqueue(self.notice())
                else:
                    self.assertTrue(controller.send_question_notice("PRIVATE-QUESTION"))
                outgoing = self.jobs[-1]
                outgoing.fail("api_rejected_minus2")
                self.assertFalse(controller._question_fault)
                incoming.cancel.assert_not_called()
                incoming.complete(self.update())
                self.assertEqual(len(received), 1)
                self.assertEqual(controller._cursor, "next-cursor")
                self.assertTrue(controller.question_channel_available())

    def test_pause_cancels_both_slots_and_resume_requires_fresh_read(self):
        controller = self.make()
        controller.set_question_channel(True)
        controller.poll_question_replies()
        incoming = self.jobs[-1]
        controller.send_question_notice("PRIVATE-QUESTION")
        outgoing = self.jobs[-1]
        received, sent = [], []
        controller.question_replies.connect(lambda *args: received.append(args))
        controller.question_send_result.connect(sent.append)
        controller.set_paused(True)
        incoming.cancel.assert_called_once()
        outgoing.cancel.assert_called_once()
        incoming.complete(self.update(cursor="old-cursor"))
        outgoing.complete(WeixinSendResult(True))
        self.assertFalse(received or sent)
        controller.set_paused(False)
        self.assertFalse(controller.question_channel_available())
        controller.poll_question_replies()
        self.jobs[-1].complete(self.update(cursor="fresh-cursor"))
        self.assertTrue(controller.question_channel_available())

    def test_disabling_question_mode_cancels_both_question_slots_together(self):
        controller = self.make()
        controller.set_question_channel(True)
        controller.poll_question_replies()
        incoming = self.jobs[-1]
        controller.send_question_notice("PRIVATE-QUESTION")
        outgoing = self.jobs[-1]
        received, sent = [], []
        controller.question_replies.connect(lambda *args: received.append(args))
        controller.question_send_result.connect(sent.append)
        controller.set_question_channel(False)
        incoming.cancel.assert_called_once()
        outgoing.cancel.assert_called_once()
        incoming.complete(self.update(cursor="stale-cursor"))
        outgoing.complete(WeixinSendResult(True))
        self.assertFalse(received or sent)
        self.assertFalse(controller._bootstrap_ready)

    def test_new_binding_requires_explicit_save_before_background_receiving_or_question_send(self):
        controller = self.make()
        controller.set_question_channel(True)
        controller.poll_question_replies()
        old_read = self.jobs[-1]
        controller.login()
        old_read.cancel.assert_called_once()
        self.assertFalse(controller._binding_committed)
        old_read.complete(self.update(cursor="old-binding-cursor"))
        self.jobs[-1].complete(WeixinQRCode("new-qr", "synthetic-qr"))
        controller.check_login()
        new_credentials = replace(CREDS, bot_token="NEW-PRIVATE-TOKEN", scanner_id="new-owner@im.wechat")
        self.jobs[-1].complete(WeixinQRResult("confirmed", DEFAULT_BASE_URL, new_credentials))
        self.assertFalse(controller._binding_committed)
        self.assertEqual(controller.question_owner_key(), "")
        controller.establish_context()
        self.assertEqual(self.jobs[-1].operation, "context")
        self.jobs[-1].complete(WeixinUpdate("UNSAVED-CONTEXT", "unsaved-cursor"))
        count = len(self.jobs)
        for _ in range(3):
            controller.set_question_channel(True)
            controller.poll_question_replies()
        self.assertEqual(len(self.jobs), count)
        self.assertFalse(controller.can_send_question())
        self.save.assert_not_called()
        controller.send_test()
        self.assertEqual(self.jobs[-1].operation, "send")
        self.jobs[-1].complete(WeixinSendResult(True))
        self.save.assert_not_called()
        controller.dialog.enabled.setChecked(False)
        controller.save_settings()
        self.assertTrue(controller._binding_committed)
        self.save.assert_called_once_with(
            controller.config_path, WeixinConfig(False, new_credentials, "UNSAVED-CONTEXT", "unsaved-cursor"),
        )
        self.assertFalse(controller._bootstrap_ready)
        controller.poll_question_replies()
        self.assertEqual(self.jobs[-1].operation, "replies")
        self.jobs[-1].complete(WeixinReplyUpdate(None, "unsaved-cursor", ()))
        self.assertTrue(controller.question_channel_available())

    def test_saved_new_binding_still_waits_for_old_receive_to_physically_finish(self):
        controller = self.make()
        controller.set_question_channel(True)
        controller.poll_question_replies()
        old_read = self.jobs[-1]
        controller.login()
        self.jobs[-1].complete(WeixinQRCode("new-qr", "synthetic-qr"))
        controller.check_login()
        credentials = replace(CREDS, bot_id="new-bot@im.bot", scanner_id="new-owner@im.wechat")
        self.jobs[-1].complete(WeixinQRResult("confirmed", DEFAULT_BASE_URL, credentials))
        controller.dialog.enabled.setChecked(False)
        controller.save_settings()
        self.assertTrue(controller._binding_committed)
        before = len(self.jobs)
        controller.poll_question_replies()
        self.assertEqual(len(self.jobs), before)
        self.assertIs(controller._reply_active, old_read)
        old_read.complete(self.update(context="OLD-OWNER-CONTEXT", cursor="old-binding-cursor"))
        self.assertEqual(controller._context, "")
        self.assertEqual(controller._cursor, "")
        controller.poll_question_replies()
        self.assertEqual(self.jobs[-1].arguments, (credentials, ""))
        self.jobs[-1].complete(WeixinReplyUpdate("NEW-OWNER-CONTEXT", "new-binding-cursor", ()))
        self.assertTrue(controller.question_channel_available())
        self.assertEqual(controller._context, "NEW-OWNER-CONTEXT")


class QuestionTransportWorkerTests(unittest.TestCase):
    def test_replies_worker_uses_only_reply_poll_and_closes_client(self):
        client = Mock()
        value = WeixinReplyUpdate(None, "cursor", ())
        client.get_reply_updates.return_value = value
        worker = WeixinWorker("replies", (CREDS, "cursor"), client_factory=lambda: client)
        received = []
        worker.result_ready.connect(received.append)
        worker.run()
        client.get_reply_updates.assert_called_once_with(CREDS, cursor="cursor")
        client.get_updates.assert_not_called()
        client.send_notice.assert_not_called()
        client.close.assert_called_once()
        self.assertEqual(received, [value])
        self.assertEqual(worker.arguments, ())

    def test_question_send_worker_uses_existing_fixed_recipient_send(self):
        client = Mock()
        client.send_notice.return_value = WeixinSendResult(True)
        worker = WeixinWorker("question_send", (CREDS, "context", "PRIVATE-QUESTION"),
                              client_factory=lambda: client)
        values = []
        worker.result_ready.connect(values.append)
        worker.run()
        client.send_notice.assert_called_once_with(CREDS, "context", "PRIVATE-QUESTION")
        client.get_reply_updates.assert_not_called()
        self.assertEqual(values, [WeixinSendResult(True)])
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
