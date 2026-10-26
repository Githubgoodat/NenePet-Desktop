"""Real encrypted synthetic question store and fake Qt WeChat transport only."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

from aipet.core.config import AppSettings
from aipet.core.weixin_notify import WeixinIncomingReply
from aipet.core.workflow_questions import QuestionStore, QuestionStoreError
from aipet.core.workflow_question_status import read_channel_status, STATUS_FILE
from aipet.ui.weixin_questions import QuestionChannelController, save_question_enabled, question_text


OWNER = "synthetic-owner-hash"


def protect(value):
    return "synthetic:" + base64.b64encode(value.encode("utf-8")).decode("ascii")


def unprotect(value):
    if not value.startswith("synthetic:"):
        raise ValueError("Invalid synthetic ciphertext")
    return base64.b64decode(value[10:], validate=True).decode("utf-8")


class Pet(QObject):
    pause_changed = pyqtSignal(bool)
    test_mode_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.settings = AppSettings()
        self.settings.character.profile_id = "nene"
        self._closing = self._paused = self._test = False

    def is_dnd_enabled(self):
        return self._paused

    def is_test_mode(self):
        return self._test


class FakeWeixin(QObject):
    question_replies = pyqtSignal(object, str)
    question_send_result = pyqtSignal(str)
    question_transport_error = pyqtSignal(str)
    slot_available = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.available = True
        self.context_ready = True
        self.enabled = False
        self.busy = False
        self.submit = True
        self.sent, self.channels = [], []
        self.epoch = self.active_epoch = 0
        self.defer_question_update = Mock()
        self.retry_question_channel = Mock()
        self.establish_context = Mock()

    def question_channel_available(self):
        return self.available

    def question_owner_key(self):
        return OWNER

    def set_question_channel(self, enabled):
        enabled = bool(enabled)
        if self.enabled and not enabled:
            self.epoch += 1
        self.enabled = enabled
        self.channels.append(enabled)

    def can_send_question(self):
        return self.available and self.context_ready and self.enabled and not self.busy

    def send_question_notice(self, text):
        if not self.can_send_question() or not self.submit:
            return False
        self.sent.append(text)
        self.busy, self.active_epoch = True, self.epoch
        return True

    def finish(self, result="accepted", *, notify_slot=True):
        self.busy = False
        if self.active_epoch == self.epoch:
            self.question_send_result.emit(result)
        if notify_slot:
            self.slot_available.emit()


class WeixinQuestionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="nene-question-ui-test-")))
        self.inbox = self.temporary / "questions"
        self.config = self.temporary / "profile" / "questions.json"
        self.now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        self.pet, self.weixin = Pet(), FakeWeixin()
        self.store = QuestionStore(self.inbox, clock=lambda: self.now, protect=protect, unprotect=unprotect)
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
            controller.dialog.close()
            controller.dialog.deleteLater()
        self.app.processEvents()

    def make(self, *, enabled=True):
        if enabled:
            save_question_enabled(self.config, True)
        controller = QuestionChannelController(
            self.pet, self.weixin, inbox=self.inbox, config_path=self.config,
            store=self.store, clock=lambda: self.now,
        )
        self.controllers.append(controller)
        return controller

    def create(self, *, source="codex", task="synthetic-task", turn="synthetic-turn", **changes):
        self.now += timedelta(seconds=1)
        values = {"source": source, "task_id": task, "turn_id": turn, "project": "Synthetic Project",
                  "question": "合成问题第一行\n请完整保留第二行。", "options": ("选项甲", "选项乙"), "timeout_seconds": 60}
        return self.store.create(**(values | changes))

    def get(self, record):
        return self.store.get(record.request_id, source=record.source, task_id=record.task_id, turn_id=record.turn_id)

    def reply(self, record, *, text=None, identity="100", created=None):
        return WeixinIncomingReply(identity, text if text is not None else "回复 " + record.code + " B",
            int(self.now.timestamp() * 1000) if created is None else created)

    def send_question(self, controller, record):
        controller.poll()
        self.assertTrue(self.get(record).delivery_attempted)
        self.assertEqual(len(self.weixin.sent), 1)

    def test_default_off_never_creates_inbox_or_contacts_network(self):
        with patch("requests.Session", side_effect=AssertionError("No real network")):
            controller = self.make(enabled=False)
            controller.poll()
            controller.retry()
        self.assertFalse(self.inbox.exists())
        self.assertFalse(self.config.exists())
        self.assertFalse(controller.timer.isActive())
        self.assertFalse(self.weixin.sent)
        self.assertFalse(self.weixin.enabled)
        self.weixin.establish_context.assert_not_called()

    def test_ready_heartbeat_then_new_question_sends_only_once(self):
        controller = self.make()
        self.assertEqual(read_channel_status(self.inbox, self.now), "ready")
        self.assertTrue((self.inbox / STATUS_FILE).exists())
        self.assertTrue(self.weixin.enabled)
        record = self.create()
        self.send_question(controller, record)
        controller.poll()
        self.weixin.finish()
        for _ in range(3):
            self.now += timedelta(seconds=6)
            controller.poll()
        self.assertEqual(len(self.weixin.sent), 1)
        self.assertEqual(read_channel_status(self.inbox, self.now), "ready")
        self.assertEqual(self.get(record).status, "pending")

    def test_enabled_idle_listener_ignores_plain_owner_text_without_sending(self):
        controller = self.make()
        self.assertTrue(self.weixin.enabled)
        self.weixin.question_replies.emit((WeixinIncomingReply("900", "开启问答", 0),), OWNER)
        controller.poll()
        self.assertTrue(self.weixin.enabled)
        self.assertEqual(self.store.list_pending(), ())
        self.assertFalse(self.weixin.sent)
        self.assertFalse(controller._receipts)
        self.assertFalse(controller._seen_replies)

    def test_question_contains_full_text_source_code_lifetime_and_options(self):
        controller = self.make()
        record = self.create(source="grok_bot", question="完整的问题\n不要省略这一行。")
        self.send_question(controller, record)
        text, = self.weixin.sent
        for expected in (record.question, record.code, "Synthetic Project", "grok bot", "60 秒",
                         "A. 选项甲", "B. 选项乙", "回复 " + record.code + " A", "不是操作授权"):
            self.assertIn(expected, text)
        for private in (record.task_id, record.turn_id, record.request_id, OWNER, str(self.inbox)):
            self.assertNotIn(private, text)
        self.assertLessEqual(len(text), 2000)

    def test_maximum_allowed_question_and_options_fit_one_message(self):
        record = self.create(question="问" * 800, options=tuple("项" * 120 for _ in range(6)))
        text = question_text(record, self.now)
        self.assertIsNotNone(text)
        self.assertIn(record.question, text)
        self.assertIn("F. " + "项" * 120, text)
        self.assertLessEqual(len(text), 2000)

    def test_exact_reply_command_only_and_first_answer_is_locked(self):
        controller = self.make()
        record = self.create()
        self.send_question(controller, record)
        for index, text in enumerate(("B", "回复" + record.code + " B", "请回复 " + record.code + " B",
                                      "回复 " + record.code, "引用 回复 " + record.code + " B")):
            controller._replies((self.reply(record, text=text, identity=str(200 + index)),), OWNER)
        self.assertEqual(self.get(record).status, "pending")
        self.weixin.question_replies.emit((self.reply(record, text="回复 " + record.code.lower() + " B"),), OWNER)
        self.assertEqual((self.get(record).status, self.get(record).answer), ("answered", "选项乙"))
        self.assertIn("等待原执行者领取", controller.status_text)
        controller._replies((self.reply(record), self.reply(record, identity="101", text="回复 " + record.code + " A")), OWNER)
        self.assertEqual(self.get(record).answer, "选项乙")
        self.assertEqual(len(controller._receipts), 1)

    def test_code_routes_answer_to_original_source_task_and_turn(self):
        controller = self.make()
        codex = self.create(task="codex-task", turn="codex-turn")
        grok = self.create(source="grok_bot", task="grok-task", turn="grok-phase")
        controller.poll()
        self.weixin.finish()
        self.weixin.finish(notify_slot=False)
        self.assertTrue(self.get(codex).delivery_attempted)
        self.assertTrue(self.get(grok).delivery_attempted)
        controller._replies((self.reply(grok, identity="300", text="回复 " + grok.code + " Grok回答"),), OWNER)
        self.assertEqual(self.get(grok).answer, "Grok回答")
        self.assertEqual(self.get(codex).status, "pending")
        controller._replies((self.reply(codex, identity="301", text="回复 " + codex.code + " Codex回答"),), OWNER)
        self.assertEqual(self.get(codex).answer, "Codex回答")
        self.assertIsNone(self.store.get(grok.request_id, source="codex", task_id=codex.task_id, turn_id=codex.turn_id))

    def test_bare_code_reply_routes_without_guessing_between_pending_tasks(self):
        controller = self.make()
        codex = self.create(task="short-code-codex", turn="short-code-turn")
        grok = self.create(source="grok_bot", task="short-code-grok", turn="short-code-phase")
        controller.poll()
        self.weixin.finish()
        self.weixin.finish(notify_slot=False)
        for index, text in enumerate(("A", grok.code, "请选 " + grok.code + " A",
                                      grok.code + "A", "引用 " + grok.code + " A")):
            controller._replies((self.reply(grok, identity=str(700 + index), text=text),), OWNER)
        self.assertEqual(self.get(grok).status, "pending")
        controller._replies((self.reply(grok, identity="710", text=grok.code.lower() + " A"),), OWNER)
        self.assertEqual(self.get(grok).answer, "选项甲")
        self.assertEqual(self.get(codex).status, "pending")
        self.assertEqual(len(controller._receipts), 1)
        controller._replies((self.reply(grok, identity="711", text=grok.code + " B"),), OWNER)
        self.assertEqual(self.get(grok).answer, "选项甲")

    def test_wrong_owner_unknown_code_and_expired_question_do_not_cross_tasks(self):
        controller = self.make()
        record = self.create()
        self.send_question(controller, record)
        controller._replies((self.reply(record, identity="400"),), "another-owner")
        self.assertEqual(self.get(record).status, "pending")
        unknown = "FFFFFFFF" if record.code != "FFFFFFFF" else "EEEEEEEE"
        controller._replies((self.reply(record, identity="401", text="回复 " + unknown + " A"),), OWNER)
        self.assertEqual(self.get(record).status, "pending")
        self.now += timedelta(seconds=60)
        controller._replies((self.reply(record, identity="402"),), OWNER)
        self.assertEqual((self.get(record).status, self.get(record).answer), ("expired", ""))
        self.assertFalse(controller._receipts)

    def test_new_owner_can_answer_new_question_with_same_message_id(self):
        controller = self.make()
        first = self.create(task="first-owner-task")
        self.send_question(controller, first)
        controller._replies((self.reply(first, identity="100"),), OWNER)
        self.weixin.finish()  # Finish the question, then its owner-bound receipt.
        self.weixin.finish()
        second_owner = "synthetic-second-owner-hash"
        with patch.object(self.weixin, "question_owner_key", return_value=second_owner):
            second = self.create(source="grok_bot", task="second-owner-task", turn="second-phase")
            controller.poll()
            self.assertTrue(self.get(second).delivery_attempted)
            self.assertEqual(self.get(second).owner_key, second_owner)
            controller._replies((self.reply(second, identity="100", text="回复 " + second.code + " A"),), second_owner)
        self.assertEqual((self.get(first).answer, self.get(first).owner_key), ("选项乙", OWNER))
        self.assertEqual((self.get(second).status, self.get(second).answer), ("answered", "选项甲"))
        self.assertIn((OWNER, "100"), controller._seen_replies)
        self.assertIn((second_owner, "100"), controller._seen_replies)

    def test_previous_owners_queued_receipt_is_discarded_after_binding_changes(self):
        controller = self.make()
        record = self.create()
        self.send_question(controller, record)
        controller._replies((self.reply(record),), OWNER)
        self.assertEqual(len(controller._receipts), 1)
        self.weixin.finish(notify_slot=False)
        with patch.object(self.weixin, "question_owner_key", return_value="synthetic-new-owner"):
            controller.poll()
            controller.poll()
        self.assertEqual(len(self.weixin.sent), 1)
        self.assertFalse(controller._receipts)
        self.assertTrue(self.weixin.enabled)
        self.assertEqual(self.get(record).answer, "选项乙")

    def test_optional_timestamps_do_not_block_valid_owner_code_first_answer(self):
        controller = self.make()
        times = (0, 2**1000, None, int((self.now + timedelta(days=1)).timestamp() * 1000))
        for index, stamp in enumerate(times):
            with self.subTest(timestamp=stamp):
                record = self.create(task=f"timestamp-task-{index}")
                self.assertTrue(self.store.claim_delivery(record.request_id, OWNER))
                first = WeixinIncomingReply(str(500 + index * 2), "回复 " + record.code + " A", stamp)
                controller._replies((first,), OWNER)
                self.assertEqual((self.get(record).status, self.get(record).answer), ("answered", "选项甲"))
                second = WeixinIncomingReply(str(501 + index * 2), "回复 " + record.code + " B", stamp)
                controller._replies((first, second), OWNER)
                self.assertEqual(self.get(record).answer, "选项甲")

    def test_busy_and_storage_error_defer_cursor_without_marking_reply_seen(self):
        for outcome in ("busy", "storage_error"):
            with self.subTest(outcome=outcome):
                controller = self.make()
                record = self.create(task="task-" + outcome)
                # Claim directly so this independent case does not compete for
                # the other controller's fake in-flight send slot.
                self.assertTrue(self.store.claim_delivery(record.request_id, OWNER))
                message = self.reply(record, identity="600-" + outcome)
                with patch.object(self.store, "submit_reply", return_value=outcome):
                    controller._replies((message,), OWNER)
                self.weixin.defer_question_update.assert_called()
                self.assertNotIn((OWNER, message.message_id), controller._seen_replies)
                self.assertEqual(self.get(record).status, "pending")
                controller._replies((message,), OWNER)
                self.assertEqual(self.get(record).answer, "选项乙")
                controller.shutdown()

    def test_unknown_send_result_or_submit_false_never_retries_question(self):
        controller = self.make()
        record = self.create()
        self.send_question(controller, record)
        self.weixin.finish("acceptance_unknown")
        controller.poll()
        self.assertEqual(len(self.weixin.sent), 1)
        self.assertTrue(self.get(record).delivery_attempted)
        self.assertIn("不明", controller.status_text)

    def test_unreadable_question_directory_defers_reply_until_reading_recovers(self):
        controller = self.make()
        record = self.create()
        self.send_question(controller, record)
        message = self.reply(record, identity="650")
        with patch.object(self.store, "list_pending", side_effect=QuestionStoreError("storage_error")):
            controller._replies((message,), OWNER)
        self.weixin.defer_question_update.assert_called_once()
        self.assertNotIn((OWNER, message.message_id), controller._seen_replies)
        self.assertEqual(self.get(record).status, "pending")
        controller._replies((message,), OWNER)
        self.assertEqual(self.get(record).answer, "选项乙")

    def test_claimed_but_not_submitted_question_is_not_automatically_retried(self):
        controller = self.make()
        record = self.create()
        self.weixin.submit = False
        controller.poll()
        self.assertTrue(self.get(record).delivery_attempted)
        self.assertFalse(self.weixin.sent)
        self.weixin.submit = True
        controller.poll()
        self.assertFalse(self.weixin.sent)

    def test_receipt_reports_only_durable_arrival_not_execution(self):
        controller = self.make()
        record = self.create()
        self.send_question(controller, record)
        controller._replies((self.reply(record),), OWNER)
        self.weixin.finish()
        self.assertEqual(len(self.weixin.sent), 2)
        receipt = self.weixin.sent[-1]
        for factual in ("回答已收到", record.code, "仍在等待时", "领取后", "不代表执行完成"):
            self.assertIn(factual, receipt)
        for false_claim in ("已经执行", "任务已完成", "已批准", "选项乙"):
            self.assertNotIn(false_claim, receipt)
        self.weixin.finish("acceptance_unknown")
        controller.poll()
        self.assertEqual(len(self.weixin.sent), 2)
        self.assertTrue(self.weixin.enabled)

    def _assert_pause_cancels_and_does_not_replay(self, mode):
        controller = self.make()
        record = self.create()
        self.send_question(controller, record)
        self.now += timedelta(seconds=1)
        if mode == "dnd":
            self.pet._paused = True
            self.pet.pause_changed.emit(True)
        elif mode == "test":
            self.pet._test = True
            self.pet.test_mode_changed.emit(True)
        elif mode == "tracking":
            controller.set_tracking(False)
        else:
            controller.shutdown()
        self.assertEqual(self.get(record).status, "cancelled")
        self.assertIsNone(controller._sending)
        self.assertFalse(self.weixin.enabled)
        controller._replies((self.reply(record),), OWNER)
        self.assertEqual(self.get(record).answer, "")
        self.assertNotEqual(read_channel_status(self.inbox, self.now), "ready")
        self.weixin.finish()  # A cancelled worker must not revive its old epoch.
        self.now += timedelta(seconds=1)
        if mode == "dnd":
            self.pet._paused = False
            self.pet.pause_changed.emit(False)
        elif mode == "test":
            self.pet._test = False
            self.pet.test_mode_changed.emit(False)
        elif mode == "tracking":
            controller.set_tracking(True)
        controller.poll()
        controller._replies((self.reply(record, identity="701"),), OWNER)
        self.assertEqual(len(self.weixin.sent), 1)
        self.assertEqual(self.get(record).status, "cancelled")
        self.assertEqual(self.weixin.enabled, mode != "shutdown")

    def test_saved_disable_stops_idle_receiver_and_cancels_pending_question(self):
        controller = self.make()
        record = self.create()
        self.send_question(controller, record)
        controller.dialog.enabled.setChecked(False)
        controller.save_settings()
        self.assertFalse(self.weixin.enabled)
        self.assertFalse(controller.timer.isActive())
        self.assertEqual(self.get(record).status, "cancelled")
        self.assertEqual(read_channel_status(self.inbox, self.now), "disabled")
        self.weixin.finish()
        controller.poll()
        self.assertFalse(self.weixin.enabled)
        self.assertEqual(len(self.weixin.sent), 1)

    def test_dnd_cancels_pending_and_sending_and_resume_never_replays(self):
        self._assert_pause_cancels_and_does_not_replay("dnd")

    def test_test_mode_cancels_pending_and_resume_never_replays(self):
        self._assert_pause_cancels_and_does_not_replay("test")

    def test_tracking_pause_cancels_pending_and_resume_never_replays(self):
        self._assert_pause_cancels_and_does_not_replay("tracking")

    def test_shutdown_cancels_pending_and_ignores_late_replies(self):
        self._assert_pause_cancels_and_does_not_replay("shutdown")

    def test_cancel_busy_during_pause_does_not_allow_old_question_answer_after_resume(self):
        controller = self.make()
        record = self.create()
        self.send_question(controller, record)
        self.now += timedelta(seconds=1)
        with patch.object(self.store, "cancel", return_value=False):
            controller.set_tracking(False)
        self.now += timedelta(seconds=1)
        with patch.object(self.store, "cancel", return_value=False):
            controller.set_tracking(True)
        controller._replies((self.reply(record, identity="800"),), OWNER)
        self.assertNotEqual(self.get(record).status, "answered")
        self.assertEqual(self.get(record).answer, "")
        self.assertFalse(controller._receipts)

    def test_storage_fault_marks_heartbeat_paused_and_retry_is_explicit(self):
        controller = self.make()
        record = self.create()
        self.now += timedelta(seconds=6)
        with patch.object(self.store, "list_pending", side_effect=QuestionStoreError("storage_error")):
            controller.poll()
        self.assertEqual(read_channel_status(self.inbox, self.now), "paused")
        self.assertFalse(self.weixin.enabled)
        self.assertFalse(self.weixin.sent)
        controller.poll()  # A recovered read alone cannot reopen a faulted channel.
        self.assertFalse(self.weixin.enabled)
        self.assertFalse(self.weixin.sent)
        self.assertFalse(self.get(record).delivery_attempted)
        controller.retry()
        self.weixin.retry_question_channel.assert_called_once()
        self.weixin.establish_context.assert_called_once()
        self.assertEqual(read_channel_status(self.inbox, self.now), "ready")

    def test_explicit_save_can_clear_storage_fault_and_reopen_idle_listener(self):
        controller = self.make()
        self.now += timedelta(seconds=6)
        with patch.object(self.store, "list_pending", side_effect=QuestionStoreError("storage_error")):
            controller.poll()
        self.assertFalse(self.weixin.enabled)
        controller.dialog.enabled.setChecked(True)
        controller.save_settings()
        self.assertTrue(self.weixin.enabled)
        self.assertFalse(controller._storage_fault)
        self.assertEqual(read_channel_status(self.inbox, self.now), "ready")


if __name__ == "__main__":
    unittest.main()
