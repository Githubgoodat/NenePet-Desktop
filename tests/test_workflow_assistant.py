"""Task tracking uses synthetic events, never actual agent tasks or API traffic."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import QApplication

from aipet.core.config import AppSettings
from aipet.core.workflow_events import TaskSnapshot
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

    def is_dnd_enabled(self):
        return self._paused

    def pause(self, value):
        self._paused = value
        self.pause_changed.emit(value)


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
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
            controller.dialog.close()
            controller.dialog.deleteLater()
        self.app.processEvents()

    def make(self):
        controller = WorkflowController(self.pet, inbox=Path("synthetic-inbox-do-not-create"),
                                        clock=lambda: self.now, loader=self.loader)
        self.controllers.append(controller)
        return controller

    def event(self, *, seconds=0, **values):
        return Event(observed_at=(self.now + timedelta(seconds=seconds)).isoformat(), **values)

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)

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


if __name__ == "__main__":
    unittest.main()
