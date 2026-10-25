"""Task tracking uses synthetic events, never actual agent tasks or API traffic."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import QApplication

from aipet.core.config import AppSettings
from aipet.core.workflow_events import TaskSnapshot, publish_hook_event
from aipet.core.workflow_input_notices import InputNotice, publish_input_notice
from aipet.ui.workflow_assistant import WorkflowController


@dataclass(frozen=True)
class Event:
    task_id: str = "fixture-session"
    source: str = "codex"
    project: str = "synthetic-project"
    event: str = "Stop"
    status: str = "turn_ended"
    observed_at: str = "2026-10-24T00:00:00+00:00"
    turn_id: str = "fixture-turn"

    def is_stale(self, now):
        return now - datetime.fromisoformat(self.observed_at) > timedelta(minutes=10)


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


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.pet = Pet()
        self.notices = []
        self.pet.notification.connect(lambda *value: self.notices.append(value))
        self.loader = Mock(return_value=())
        self.input_loader = Mock(return_value=())
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
            controller.dialog.close()
            controller.dialog.deleteLater()
        self.app.processEvents()

    def make(self):
        controller = WorkflowController(self.pet, inbox=Path("synthetic-inbox-do-not-create"),
                                        clock=lambda: self.now, loader=self.loader, input_loader=self.input_loader)
        self.controllers.append(controller)
        return controller

    def event(self, *, seconds=0, **values):
        return Event(observed_at=(self.now + timedelta(seconds=seconds)).isoformat(), **values)

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)

    def input_notice(self, *, identity="question-one", seconds=0, status="input_requested", **values):
        return InputNotice(
            notice_id=hashlib.sha256(identity.encode()).hexdigest(), task_id="fixture-session",
            turn_id="fixture-turn", project=values.pop("project", "synthetic-project"),
            status=status, observed_at=(self.now + timedelta(seconds=seconds)).isoformat(), **values,
        )

    def test_startup_only_builds_baseline_and_never_claims_success(self):
        self.loader.return_value = (self.event(seconds=-2),)
        controller = self.make()
        self.assertEqual(self.notices, [])
        self.assertEqual(controller.dialog.table.rowCount(), 1)
        state = controller.dialog.table.item(0, 3).text()
        self.assertEqual(state, "本轮结束，待查看")
        self.assertNotIn("完成", state)
        self.assertIn("未连接", controller.dialog.sources.text().splitlines()[1])

    def test_inbox_path_can_be_selected_without_touching_clipboard(self):
        with patch.object(QApplication, "clipboard", side_effect=AssertionError("No automatic clipboard access")):
            controller = self.make()
        self.assertIn(str(controller.inbox), controller.dialog.inbox_path.text())
        self.assertTrue(controller.dialog.inbox_path.textInteractionFlags() & Qt.TextSelectableByMouse)
        self.assertEqual(controller.dialog.inbox_path.textFormat(), Qt.PlainText)
        self.assertEqual(controller.dialog.bark.text(), "iPhone备用提醒…")
        self.assertEqual(controller.dialog.bark_status.textFormat(), Qt.PlainText)
        self.assertIn("未启用", controller.dialog.bark_status.text())

    def test_new_attention_and_turn_end_are_notified_once(self):
        controller = self.make()
        for event, status in (("PermissionRequest", "needs_attention"), ("Stop", "turn_ended"),
                              ("Interrupt", "interrupted")):
            self.advance()
            self.loader.return_value = (self.event(event=event, status=status),)
            controller.poll()
            controller.poll()
        self.assertEqual(len(self.notices), 3)
        self.assertIn("不能证明任务完成", self.notices[1][1])

    def test_running_updates_are_visible_without_interrupting_user(self):
        controller = self.make()
        self.advance()
        self.loader.return_value = (self.event(event="PreToolUse", status="running"),)
        controller.poll()
        self.assertEqual(self.notices, [])
        self.assertIn("正在处理", controller.dialog.table.item(0, 3).text())

    def test_real_snapshot_contract_and_utc_z_timestamp(self):
        controller = self.make()
        self.advance()
        timestamp = self.now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.loader.return_value = (TaskSnapshot("fixture-real", "codex", "fixture", "Stop",
                                                  "turn_ended", timestamp, "fixture-turn"),)
        controller.poll()
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(controller.dialog.table.item(0, 3).text(), "本轮结束，待查看")

    def test_real_reader_never_creates_missing_inbox(self):
        with patch.object(Path, "mkdir", side_effect=AssertionError("Reader must not create directories")):
            controller = WorkflowController(self.pet, inbox=Path("fixture-inbox-must-not-exist"),
                                            clock=lambda: self.now)
            self.controllers.append(controller)
            controller.poll()
            self.assertEqual(controller.dialog.table.rowCount(), 0)
        self.assertEqual(self.notices, [])

    def test_closing_panel_keeps_following_without_opening_it_again(self):
        controller = self.make()
        controller.dialog.close()
        self.advance()
        self.loader.return_value = (self.event(),)
        controller.poll()
        self.assertTrue(controller.timer.isActive())
        self.assertFalse(controller.dialog.isVisible())
        self.assertEqual(len(self.notices), 1)

    def test_delayed_old_snapshot_cannot_regress_newer_state_or_notify(self):
        controller = self.make()
        old = self.event(status="running", event="UserPromptSubmit")
        self.advance(2)
        current = self.event()
        self.loader.return_value = (current,)
        controller.poll()
        self.loader.return_value = (old,)
        controller.poll()
        self.assertEqual(controller._snapshots[(current.source, current.task_id)], current)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(controller.dialog.table.item(0, 3).text(), "本轮结束，待查看")

    def test_old_first_seen_event_and_unknown_or_future_source_do_not_notify(self):
        controller = self.make()
        self.advance(3)
        self.loader.return_value = (self.event(seconds=-4), self.event(task_id="grok", source="grok"),
                                    self.event(task_id="future", seconds=20))
        controller.poll()
        self.assertEqual(self.notices, [])
        self.assertEqual(controller.dialog.table.rowCount(), 1)

    def test_stale_does_not_mean_failed_or_trigger_late_notice(self):
        controller = self.make()
        record = self.event(seconds=1)
        self.advance(602)
        self.loader.return_value = (record,)
        controller.poll()
        self.assertEqual(self.notices, [])
        self.assertEqual(controller.dialog.table.item(0, 3).text(), "状态已过期，请回来源确认")

    def test_dnd_stops_reading_and_resume_does_not_replay_events(self):
        controller = self.make()
        self.pet.pause(True)
        self.assertFalse(controller.timer.isActive())
        reads = self.loader.call_count
        self.advance()
        self.loader.return_value = (self.event(),)
        controller.poll()
        self.assertEqual(self.loader.call_count, reads)
        self.pet.pause(False)
        self.assertTrue(controller.timer.isActive())
        self.assertEqual(self.notices, [])
        self.advance()
        self.loader.return_value = (self.event(event="PermissionRequest", status="needs_attention"),)
        controller.poll()
        self.assertEqual(len(self.notices), 1)

    def test_initial_dnd_never_scans_or_opens_window(self):
        self.pet._paused = True
        controller = self.make()
        controller.open()
        self.loader.assert_not_called()
        self.assertFalse(controller.dialog.isVisible())
        self.assertFalse(controller.timer.isActive())

    def test_manual_pause_and_test_mode_switch_rebuild_baseline(self):
        controller = self.make()
        controller.set_enabled(False)
        reads = self.loader.call_count
        self.advance()
        self.loader.return_value = (self.event(),)
        controller.poll()
        self.assertEqual(self.loader.call_count, reads)
        controller.set_enabled(True)
        self.assertEqual(self.notices, [])
        self.advance()
        self.loader.return_value = (self.event(event="PermissionRequest", status="needs_attention"),)
        self.pet.test_mode_changed.emit(True)
        self.assertEqual(self.notices, [])

    def test_shutdown_prevents_late_reads_notices_and_windows(self):
        controller = self.make()
        controller.shutdown()
        reads = self.loader.call_count
        self.advance()
        self.loader.return_value = (self.event(),)
        controller.poll()
        controller.open()
        self.pet.pause_changed.emit(False)
        self.pet.test_mode_changed.emit(True)
        self.assertEqual(self.loader.call_count, reads)
        self.assertFalse(controller.timer.isActive())
        self.assertFalse(controller.dialog.isVisible())
        self.assertEqual(self.notices, [])

    def test_loader_error_keeps_last_evidence_and_recovery_is_quiet(self):
        self.loader.return_value = (self.event(seconds=-2, status="running", event="PreToolUse"),)
        controller = self.make()
        self.loader.side_effect = OSError("fixture only")
        controller.poll()
        self.assertIn("来源暂不可读", controller.dialog.table.item(0, 3).text())
        self.assertIn("状态未知", controller.dialog.status.text())
        self.loader.side_effect = None
        self.advance()
        self.loader.return_value = (self.event(),)
        controller.poll()
        self.assertEqual(self.notices, [])

    def test_other_profile_does_not_read_or_start_followup_work(self):
        controller = self.make()
        self.pet.settings.character.profile_id = "murasame"
        reads = self.loader.call_count
        controller.poll()
        self.assertEqual(self.loader.call_count, reads)
        self.assertEqual(self.notices, [])

    def test_grok_requires_real_collaborative_event_and_keeps_source_identity(self):
        controller = self.make()
        self.assertIn("未连接或尚无协作上报", controller.dialog.sources.text())
        self.advance()
        self.loader.return_value = (
            self.event(source="codex", task_id="same-id"),
            self.event(source="grok_bot", task_id="same-id", event="GrokPhaseEnded", status="phase_ended"),
        )
        controller.poll()
        self.assertEqual(controller.dialog.table.rowCount(), 2)
        self.assertEqual(len(controller._snapshots), 2)
        self.assertIn("已收到协作上报（非原生订阅", controller.dialog.sources.text())
        self.assertEqual({controller.dialog.table.item(row, 3).text() for row in range(2)},
                         {"本轮结束，待查看", "阶段结束，待查看"})
        self.assertEqual(len(self.notices), 2)

    def test_grok_failed_is_source_report_not_a_task_success_claim(self):
        controller = self.make()
        self.advance()
        self.loader.return_value = (self.event(source="grok_bot", event="GrokFailed", status="failed"),)
        controller.poll()
        self.assertIn("来源上报失败", controller.dialog.table.item(0, 3).text())
        self.assertIn("协作方上报失败", self.notices[0][1])

    def test_notification_signal_has_same_freshness_and_pause_filter_as_local_notice(self):
        controller = self.make()
        ready = []
        tracking = []
        controller.notification_ready.connect(ready.append)
        controller.tracking_changed.connect(tracking.append)
        self.advance()
        record = self.event()
        self.loader.return_value = (record,)
        controller.poll()
        controller.poll()
        self.assertEqual(ready, [record])
        self.pet.pause(True)
        self.advance()
        self.loader.return_value = (self.event(source="grok_bot", status="needs_attention", event="GrokNeedsInput"),)
        controller.poll()
        self.pet.pause(False)
        self.assertEqual(ready, [record])
        controller.set_enabled(False)
        controller.set_enabled(True)
        controller.shutdown()
        self.assertEqual(tracking, [False, True, False, True, False])

    def test_durable_question_is_not_lost_when_running_overwrites_task_state_before_poll(self):
        controller = self.make()
        ready = []
        controller.notification_ready.connect(ready.append)
        self.advance()
        question = self.input_notice()
        self.advance(2)
        self.loader.return_value = (self.event(event="PreToolUse", status="running"),)
        self.input_loader.return_value = (question,)
        controller.poll()
        controller.poll()
        self.assertEqual(ready, [question])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("发起输入请求", self.notices[0][1])
        self.assertIn("正在处理", controller.dialog.table.item(0, 3).text())
        self.assertIn("不判断是否已回答", controller.dialog.input_summary.text())
        self.assertEqual(controller.dialog.input_summary.textFormat(), Qt.PlainText)

    def test_real_file_channels_keep_question_after_following_running_event(self):
        with TemporaryDirectory(prefix="workflow-ui-input-") as temporary:
            inbox = Path(temporary).resolve() / "synthetic-inbox"
            controller = WorkflowController(self.pet, inbox=inbox, clock=lambda: self.now)
            self.controllers.append(controller)
            ready = []
            controller.notification_ready.connect(ready.append)
            payload = {
                "hook_event_name": "PreToolUse", "session_id": "synthetic-ui-session",
                "turn_id": "synthetic-ui-turn", "tool_use_id": "synthetic-ui-question",
                "tool_name": "request_user_input_async", "cwd": "C:\\Synthetic\\UI Fixture",
                "tool_input": {"questions": "PRIVATE QUESTION CONTENT"},
            }
            self.advance()
            self.assertTrue(publish_hook_event(inbox, payload, now=self.now))
            self.assertTrue(publish_input_notice(inbox, payload, now=self.now))
            self.advance()
            running = dict(payload, tool_name="Bash", tool_use_id="synthetic-following-tool")
            self.assertTrue(publish_hook_event(inbox, running, now=self.now))
            self.advance()
            controller.poll()
            controller.poll()
            self.assertEqual(len(ready), 1)
            self.assertIsInstance(ready[0], InputNotice)
            self.assertEqual(ready[0].status, "question_opened")
            self.assertIn("正在处理", controller.dialog.table.item(0, 3).text())
            self.assertIn("可能仍在继续", self.notices[0][1])
            self.assertNotIn("PRIVATE", str(self.notices))

    def test_two_distinct_questions_at_same_time_are_independent_and_async_is_not_pause(self):
        controller = self.make()
        ready = []
        controller.notification_ready.connect(ready.append)
        self.advance()
        sync = self.input_notice(identity="same-time-sync")
        asynchronous = self.input_notice(identity="same-time-async", status="question_opened")
        self.input_loader.return_value = (asynchronous, sync)
        controller.poll()
        controller.poll()
        self.assertEqual({item.notice_id for item in ready}, {sync.notice_id, asynchronous.notice_id})
        self.assertEqual(len(self.notices), 2)
        async_text = next(body for _, body in self.notices if "可能仍在继续" in body)
        self.assertNotIn("暂停", async_text)

    def test_question_channel_is_notified_before_a_burst_of_turn_endings(self):
        controller = self.make()
        ready = []
        controller.notification_ready.connect(ready.append)
        self.advance()
        question = self.input_notice(identity="before-turn-burst")
        self.input_loader.return_value = (question,)
        endings = tuple(self.event(task_id=f"ending-{index}") for index in range(6))
        self.loader.return_value = endings
        controller.poll()
        self.assertEqual(ready[0], question)
        self.assertEqual(ready[1:], list(endings))
        self.assertEqual(controller.dialog.table.rowCount(), 6)

    def test_question_startup_baseline_and_immutable_identity_prevent_replay(self):
        old = self.input_notice(seconds=-1)
        self.input_loader.return_value = (old,)
        controller = self.make()
        self.assertFalse(self.notices)
        self.advance()
        fresh = self.input_notice(identity="fresh")
        self.input_loader.return_value = (fresh, old)
        controller.poll()
        self.advance()
        changed_duplicate = self.input_notice(identity="fresh", status="question_opened")
        self.input_loader.return_value = (changed_duplicate,)
        controller.poll()
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(controller._input_notices[fresh.notice_id], fresh)

    def test_dnd_resume_questions_are_baselined_without_reads_or_backlog(self):
        controller = self.make()
        self.pet.pause(True)
        reads = self.input_loader.call_count
        self.advance()
        self.input_loader.return_value = (self.input_notice(identity="during-dnd"),)
        controller.poll()
        self.assertEqual(self.input_loader.call_count, reads)
        self.pet.pause(False)
        self.assertFalse(self.notices)
        self.advance()
        self.input_loader.return_value += (self.input_notice(identity="after-dnd"),)
        controller.poll()
        self.assertEqual(len(self.notices), 1)

    def test_pause_and_sustained_test_mode_do_not_read_questions_and_resume_is_quiet(self):
        controller = self.make()
        controller.set_enabled(False)
        self.advance()
        self.input_loader.return_value = (self.input_notice(identity="paused"),)
        reads = self.input_loader.call_count
        controller.poll()
        self.assertEqual(self.input_loader.call_count, reads)
        controller.set_enabled(True)
        self.pet.test_mode_changed.emit(True)
        self.assertFalse(controller.timer.isActive())
        reads = self.input_loader.call_count
        self.advance()
        self.input_loader.return_value += (self.input_notice(identity="test-mode"),)
        controller.poll()
        self.assertEqual(self.input_loader.call_count, reads)
        self.pet.test_mode_changed.emit(False)
        self.assertFalse(self.notices)
        self.assertTrue(controller.timer.isActive())
        self.advance()
        self.input_loader.return_value += (self.input_notice(identity="formal-after-test"),)
        controller.poll()
        self.assertEqual(len(self.notices), 1)

    def test_question_reader_is_independent_when_lifecycle_reader_fails(self):
        controller = self.make()
        self.loader.side_effect = OSError("synthetic lifecycle unavailable")
        self.advance()
        self.input_loader.return_value = (self.input_notice(),)
        controller.poll()
        self.assertEqual(len(self.notices), 1)
        self.assertIn("状态未知", controller.dialog.status.text())

    def test_question_read_failure_preserves_seen_and_recovers_with_quiet_baseline(self):
        controller = self.make()
        self.advance()
        first = self.input_notice()
        self.input_loader.return_value = (first,)
        controller.poll()
        self.input_loader.side_effect = OSError("synthetic question unavailable")
        self.advance()
        controller.poll()
        self.assertIn(first.notice_id, controller._input_seen)
        self.assertIn("暂不可读", controller.dialog.input_summary.text())
        missed = self.input_notice(identity="during-error")
        self.input_loader.side_effect = None
        self.advance()
        self.input_loader.return_value = (first, missed)
        controller.poll()
        self.assertEqual(len(self.notices), 1)
        self.advance()
        self.input_loader.return_value += (self.input_notice(identity="after-error"),)
        controller.poll()
        self.assertEqual(len(self.notices), 2)

    def test_delayed_independent_question_notices_are_delivered_once_without_regressing_summary(self):
        controller = self.make()
        self.advance()
        older = self.input_notice(identity="older", project="older-project")
        self.advance()
        newer = self.input_notice(identity="newer", project="newer-project")
        self.input_loader.return_value = (newer,)
        controller.poll()
        self.advance()
        self.input_loader.return_value = (older, newer)
        controller.poll()
        self.assertEqual(len(self.notices), 2)
        self.assertIn("newer-project", controller.dialog.input_summary.text())

    def test_old_stale_unknown_and_future_question_notices_do_not_notify(self):
        controller = self.make()
        self.advance(3)
        self.input_loader.return_value = (
            self.input_notice(identity="before-watch", seconds=-4),
            self.input_notice(identity="stale", seconds=-601),
            self.input_notice(identity="unknown-source", source="unknown"),
            self.input_notice(identity="unknown-state", status="unknown"),
            self.input_notice(identity="future", seconds=20),
        )
        controller.poll()
        self.assertFalse(self.notices)
        self.assertEqual(len(controller._input_notices), 2)

    def test_question_dedup_is_bounded_and_pruned_old_events_cannot_replay(self):
        controller = self.make()
        first = None
        for index in range(270):
            self.advance()
            record = self.input_notice(identity=f"bounded-{index}")
            first = first or record
            self.input_loader.return_value = (record,)
            controller.poll()
        self.assertEqual(len(self.notices), 270)
        self.assertLessEqual(len(controller._input_seen), 256)
        self.input_loader.return_value = (first,)
        controller.poll()
        self.assertEqual(len(self.notices), 270)

    def test_initial_test_mode_and_shutdown_do_not_read_question_channel(self):
        self.pet._test_mode = True
        controller = self.make()
        self.input_loader.assert_not_called()
        controller.shutdown()
        controller.poll()
        self.pet.test_mode_changed.emit(False)
        self.input_loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
