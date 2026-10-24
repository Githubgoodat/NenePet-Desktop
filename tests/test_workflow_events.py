"""Synthetic lifecycle events only: no Codex sessions, accounts or services."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aipet.core.workflow_events import (
    EVENT_STATUSES, GROK_EVENT_STATUSES, MAX_SNAPSHOTS, MAX_SNAPSHOT_BYTES, PREFIX,
    load_snapshots, publish_hook_event, publish_task_event,
)

_HOOK_PATH = Path(__file__).resolve().parents[1] / "tools/nene_workflow_hook.py"
_SPEC = importlib.util.spec_from_file_location("nene_workflow_hook_fixture", _HOOK_PATH)
hook = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hook)
_EVENT_SPEC = importlib.util.spec_from_file_location("nene_workflow_event_fixture", _HOOK_PATH.with_name("nene_workflow_event.py"))
event_cli = importlib.util.module_from_spec(_EVENT_SPEC)
_EVENT_SPEC.loader.exec_module(event_cli)


class WorkflowEventTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="nenepet-workflow-test-")))
        self.inbox = self.root / "events"
        self.now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

    def payload(self, event="UserPromptSubmit", *, session="synthetic-session", turn="synthetic-turn"):
        return {"hook_event_name": event, "session_id": session, "turn_id": turn,
                "cwd": "C:\\ExampleWorkspace\\Synthetic Project",
                "prompt": "PRIVATE PROMPT SENTINEL", "last_assistant_message": "PRIVATE ANSWER SENTINEL",
                "transcript_path": "C:\\private\\transcript.jsonl",
                "tool_input": {"command": "never execute this"}, "tool_response": "PRIVATE OUTPUT"}

    def publish(self, event="UserPromptSubmit", *, now=None, session="synthetic-session", turn="synthetic-turn"):
        return publish_hook_event(self.inbox, self.payload(event, session=session, turn=turn), now=now or self.now)

    def files(self):
        return list(self.inbox.glob(PREFIX + "*.json"))

    def test_all_event_projections_are_factual_not_success_claims(self):
        for index, (event, expected) in enumerate(EVENT_STATUSES.items()):
            with self.subTest(event=event):
                self.assertTrue(self.publish(event, session=f"fixture-{index}"))
        snapshots = load_snapshots(self.inbox, self.now)
        self.assertEqual(len(snapshots), len(EVENT_STATUSES))
        self.assertEqual({item.event: item.status for item in snapshots}, EVENT_STATUSES)
        self.assertNotIn("success", {item.status for item in snapshots})

    def test_metadata_projection_drops_conversation_tool_and_path_contents(self):
        self.assertTrue(self.publish())
        saved = self.files()[0].read_text(encoding="utf-8")
        self.assertLess(len(saved.encode("utf-8")), MAX_SNAPSHOT_BYTES)
        self.assertNotIn("PRIVATE", saved)
        self.assertNotIn("transcript", saved)
        self.assertNotIn("ExampleWorkspace", saved)
        self.assertNotIn("never execute", saved)
        snapshot, = load_snapshots(self.inbox, self.now)
        self.assertEqual(snapshot.project, "Synthetic Project")
        self.assertEqual(snapshot.task_id, "synthetic-session")
        self.assertEqual(snapshot.turn_id, "synthetic-turn")
        self.assertEqual(snapshot.source, "codex")
        self.assertEqual(snapshot.observed_at, "2026-10-24T12:00:00.000000Z")

    def test_reader_creates_no_directory_or_lock_file(self):
        self.assertEqual(load_snapshots(self.inbox, self.now), ())
        self.assertFalse(self.inbox.exists())
        self.inbox.mkdir()
        self.assertEqual(load_snapshots(self.inbox, self.now), ())
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_latest_turn_per_session_and_newest_task_first(self):
        self.assertTrue(self.publish(turn="old-turn"))
        self.assertTrue(self.publish("Stop", turn="new-turn", now=self.now + timedelta(seconds=1)))
        self.assertTrue(self.publish(session="second-task", now=self.now + timedelta(seconds=2)))
        snapshots = load_snapshots(self.inbox, self.now)
        self.assertEqual([item.task_id for item in snapshots], ["second-task", "synthetic-session"])
        self.assertEqual(snapshots[1].turn_id, "new-turn")
        self.assertEqual(len(self.files()), 3)

    def test_stale_after_ten_minutes_is_not_failure(self):
        self.publish()
        snapshot, = load_snapshots(self.inbox, self.now)
        self.assertFalse(snapshot.is_stale(self.now + timedelta(seconds=599)))
        self.assertTrue(snapshot.is_stale(self.now + timedelta(minutes=10)))
        stale, = load_snapshots(self.inbox, self.now + timedelta(days=1))
        self.assertEqual(stale.status, "running")
        self.assertTrue(stale.is_stale(self.now + timedelta(days=1)))

    def test_future_more_than_sixty_seconds_rejected(self):
        self.publish(now=self.now + timedelta(seconds=61))
        self.assertEqual(load_snapshots(self.inbox, self.now), ())
        self.assertEqual(len(load_snapshots(self.inbox, self.now + timedelta(seconds=1))), 1)

    def test_older_or_late_nonterminal_event_does_not_overwrite_newer_terminal(self):
        terminal_time = self.now + timedelta(seconds=10)
        self.assertTrue(self.publish("Stop", now=terminal_time))
        original = self.files()[0].read_bytes()
        self.assertFalse(self.publish("PermissionRequest", now=self.now))
        self.assertFalse(self.publish("PreToolUse", now=terminal_time + timedelta(seconds=1)))
        self.assertEqual(self.files()[0].read_bytes(), original)

    def test_reader_rejects_unknown_source_unknown_fields_and_wrong_filename(self):
        self.publish()
        path = self.files()[0]
        original = json.loads(path.read_bytes())
        for change in ({"source": "unrecognized-bot"}, {"prompt": "must not load"},
                       {"schema_version": True}, {"status": "success"},
                       {"observed_at": "2026-10-24T12:00:00"}, {"project_basename": "C:/private"}):
            with self.subTest(change=change):
                path.write_text(json.dumps(original | change), encoding="utf-8")
                self.assertEqual(load_snapshots(self.inbox, self.now), ())
        path.write_text(json.dumps(original), encoding="utf-8")
        path.rename(self.inbox / (PREFIX + "0" * 64 + ".json"))
        self.assertEqual(load_snapshots(self.inbox, self.now), ())

    def test_oversized_invalid_and_unrelated_files_are_untouched(self):
        self.inbox.mkdir()
        unrelated = self.inbox / "my-extension.json"
        unrelated.write_text("preserve this file", encoding="utf-8")
        oversized = self.inbox / (PREFIX + "1" * 64 + ".json")
        oversized.write_bytes(b" " * MAX_SNAPSHOT_BYTES)
        self.assertEqual(load_snapshots(self.inbox, self.now), ())
        self.assertTrue(self.publish())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve this file")
        self.assertEqual(oversized.stat().st_size, MAX_SNAPSHOT_BYTES)

    def test_invalid_hook_ids_and_events_do_not_create_inbox(self):
        for change in ({"session_id": "../private"}, {"turn_id": ""},
                       {"hook_event_name": "UnknownEvent"}, {"session_id": 12}):
            with self.subTest(change=change):
                self.assertFalse(publish_hook_event(self.inbox, self.payload() | change, now=self.now))
                self.assertFalse(self.inbox.exists())
        payload = self.payload("SessionEnd")
        payload.pop("turn_id")
        self.assertTrue(publish_hook_event(self.inbox, payload, now=self.now))
        self.assertEqual(load_snapshots(self.inbox, self.now)[0].turn_id, "")

    def test_capacity_eviction_preserves_unrelated_files_and_live_tasks(self):
        for index in range(MAX_SNAPSHOTS):
            self.assertTrue(self.publish("Stop", session=f"ended-{index}", now=self.now + timedelta(microseconds=index)))
        unrelated = self.inbox / "keep-me.txt"
        unrelated.write_text("user-owned", encoding="utf-8")
        self.assertTrue(self.publish(session="latest", now=self.now + timedelta(seconds=1)))
        self.assertEqual(len(self.files()), MAX_SNAPSHOTS)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "user-owned")
        self.assertEqual(load_snapshots(self.inbox, self.now)[0].task_id, "latest")

    def test_nonblocking_lock_collision_silently_skips_and_later_recovers(self):
        self.publish()
        from aipet.core.workflow_events import _inbox_lock
        with _inbox_lock(self.inbox.resolve()) as locked:
            self.assertTrue(locked)
            self.assertFalse(self.publish(now=self.now + timedelta(seconds=1)))
        self.assertTrue(self.publish(now=self.now + timedelta(seconds=2)))

    def test_concurrent_snapshots_are_complete_and_preserve_newest_accepted_event(self):
        self.inbox.mkdir()
        def update(index):
            time = self.now + timedelta(microseconds=index)
            return index, self.publish(now=time)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(update, range(1, 13)))
        accepted = [index for index, good in results if good]
        self.assertTrue(accepted)
        snapshot, = load_snapshots(self.inbox, self.now)
        expected = (self.now + timedelta(microseconds=max(accepted))).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.assertEqual(snapshot.observed_at, expected)
        json.loads(self.files()[0].read_bytes())

    def test_hook_is_silent_success_for_bad_input_args_and_write_errors(self):
        output, errors = io.StringIO(), io.StringIO()
        for arguments, raw in (([], "{}"), (["--unknown"], "{}"),
                               (["--inbox", "relative-inbox"], "{}"),
                               (["--inbox", str(self.inbox)], "not-json"),
                               (["--inbox", str(self.inbox)], "x" * (hook.MAX_INPUT_BYTES + 1))):
            with self.subTest(arguments=arguments), redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(hook.main(arguments, stdin=io.StringIO(raw)), 0)
        with patch.object(hook, "publish_hook_event", side_effect=OSError("fixture inaccessible")), \
                redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(hook.main(["--inbox", str(self.inbox)], stdin=io.StringIO(json.dumps(self.payload()))), 0)
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [{}] * 6)
        self.assertEqual(errors.getvalue(), "")
        self.assertFalse(self.inbox.exists())

    def test_hook_accepts_bytes_input_without_invoking_unrelated_services(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), \
                patch("subprocess.Popen", side_effect=AssertionError("No process launches")), \
                patch("socket.socket", side_effect=AssertionError("No network")):
            self.assertEqual(hook.main(["--inbox", str(self.inbox)],
                stdin=io.BytesIO(json.dumps(self.payload()).encode("utf-8"))), 0)
        self.assertEqual(len(load_snapshots(self.inbox)), 1)
        self.assertEqual(json.loads(output.getvalue()), {})
        self.assertEqual(errors.getvalue(), "")

    def test_grok_cooperation_events_have_fixed_source_and_phase_status(self):
        for index, (event, status) in enumerate(GROK_EVENT_STATUSES.items()):
            self.assertTrue(publish_task_event(self.inbox, task_id=f"grok-fixture-{index}",
                phase_id="phase-one", event=event, project="Synthetic Project", now=self.now))
        snapshots = load_snapshots(self.inbox, self.now)
        self.assertEqual({item.event: item.status for item in snapshots}, GROK_EVENT_STATUSES)
        self.assertEqual({item.source for item in snapshots}, {"grok_bot"})
        self.assertEqual({item.turn_id for item in snapshots}, {"phase-one"})
        self.assertNotIn("success", {item.status for item in snapshots})

    def test_codex_and_grok_identical_ids_do_not_collide_or_hide_each_other(self):
        self.assertTrue(self.publish())
        self.assertTrue(publish_task_event(self.inbox, task_id="synthetic-session", phase_id="synthetic-turn",
            event="GrokNeedsInput", project="Synthetic Project", now=self.now))
        self.assertEqual(len(self.files()), 2)
        self.assertEqual({item.source for item in load_snapshots(self.inbox, self.now)}, {"codex", "grok_bot"})

    def test_grok_source_event_pair_and_project_labels_are_strict(self):
        for change in ({"event": "Stop"}, {"project": "C:\\private\\task"},
                       {"phase_id": ""}, {"task_id": "../../escape"}, {"project": "unsafe\nlabel"}):
            values = {"task_id": "fixture", "phase_id": "phase", "event": "GrokRunning", "project": "Example"} | change
            self.assertFalse(publish_task_event(self.inbox, **values, now=self.now))
            self.assertFalse(self.inbox.exists())
        self.assertFalse(publish_hook_event(self.inbox, self.payload("GrokRunning"), now=self.now))
        self.assertFalse(self.inbox.exists())

    def test_grok_ended_phase_cannot_regress_but_new_phase_can_run(self):
        common = {"task_id": "grok-fixture", "project": "Example"}
        self.assertTrue(publish_task_event(self.inbox, **common, phase_id="phase-one", event="GrokPhaseEnded", now=self.now))
        self.assertFalse(publish_task_event(self.inbox, **common, phase_id="phase-one", event="GrokRunning",
                                          now=self.now + timedelta(seconds=1)))
        self.assertTrue(publish_task_event(self.inbox, **common, phase_id="phase-two", event="GrokRunning",
                                         now=self.now + timedelta(seconds=2)))
        snapshot, = load_snapshots(self.inbox, self.now)
        self.assertEqual((snapshot.turn_id, snapshot.status), ("phase-two", "running"))

    def test_grok_cli_only_reports_local_acceptance_and_never_observes_or_executes(self):
        args = ["--inbox", str(self.inbox), "--task-id", "fixture", "--phase-id", "phase",
                "--event", "needs-input", "--project", "Example"]
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), \
                patch("subprocess.Popen", side_effect=AssertionError("No process launches")), \
                patch("socket.socket", side_effect=AssertionError("No network")):
            self.assertEqual(event_cli.main(args), 0)
        self.assertEqual(json.loads(output.getvalue()), {"accepted": True})
        self.assertEqual(errors.getvalue(), "")
        snapshot, = load_snapshots(self.inbox)
        self.assertEqual((snapshot.source, snapshot.status), ("grok_bot", "needs_attention"))
        for invalid in ([], ["--inbox", "relative", *args[2:]], [*args[:-3], "success", "--project", "Example"]):
            with self.subTest(arguments=invalid), redirect_stdout(io.StringIO()) as ignored, redirect_stderr(io.StringIO()) as error:
                self.assertEqual(event_cli.main(invalid), 0)
            self.assertEqual(json.loads(ignored.getvalue()), {"accepted": False})
            self.assertEqual(error.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
