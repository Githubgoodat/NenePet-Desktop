"""Synthetic input metadata only; no conversations, services or subprocesses."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aipet.core import workflow_input_notices as notices
from aipet.core.workflow_events import load_snapshots, publish_hook_event
from aipet.core.workflow_input_notices import load_input_notices, publish_input_notice


class WorkflowInputNoticeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="nene-input-notice-test-")))
        self.inbox = self.root / "events"
        self.directory = self.inbox / notices.DIRECTORY
        self.now = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)

    def payload(self, tool="request_user_input", *, question="synthetic-question", turn="synthetic-turn"):
        return {
            "hook_event_name": "PreToolUse", "tool_name": tool,
            "session_id": "synthetic-session", "turn_id": turn, "tool_use_id": question,
            "cwd": "C:\\PrivateWorkspace\\Synthetic Project",
            "prompt": "PRIVATE PROMPT SENTINEL", "last_assistant_message": "PRIVATE ANSWER SENTINEL",
            "transcript_path": "C:\\PrivateWorkspace\\do-not-open.jsonl",
            "tool_input": {"question": "PRIVATE QUESTION SENTINEL"},
            "tool_response": "PRIVATE RESPONSE SENTINEL", "token": "PRIVATE TOKEN SENTINEL",
        }

    def publish(self, tool="request_user_input", *, question="synthetic-question", turn="synthetic-turn", now=None):
        return publish_input_notice(self.inbox, self.payload(tool, question=question, turn=turn), now=now or self.now)

    def files(self):
        return list(self.directory.glob(notices.PREFIX + "*.json"))

    def record(self):
        path, = self.files()
        return path, json.loads(path.read_text(encoding="utf-8"))

    def test_exact_synchronous_and_asynchronous_tools_have_distinct_factual_states(self):
        expected = {
            "request_user_input": "input_requested", "functions.request_user_input": "input_requested",
            "request_user_input_async": "question_opened", "functions.request_user_input_async": "question_opened",
        }
        for index, (tool, state) in enumerate(expected.items()):
            with self.subTest(tool=tool):
                self.assertTrue(self.publish(tool, question=f"question-{index}"))
        rows = load_input_notices(self.inbox, self.now)
        self.assertEqual(len(rows), 4)
        by_question = {json.loads(path.read_bytes())["tool_use_id"]: json.loads(path.read_bytes())["status"]
                       for path in self.files()}
        self.assertEqual(by_question, {f"question-{index}": state for index, state in enumerate(expected.values())})
        self.assertEqual({row.source for row in rows}, {"codex"})
        self.assertNotIn("paused", {row.status for row in rows})
        with self.assertRaises(FrozenInstanceError):
            rows[0].status = "paused"

    def test_unknown_tools_and_tools_hidden_in_input_do_not_trigger(self):
        for tool in ("Bash", "request_user_input_extra", "request_user_input_async_extra",
                     "other.request_user_input", "REQUEST_USER_INPUT", [], None):
            with self.subTest(tool=tool):
                payload = self.payload() | {"tool_name": tool,
                                           "tool_input": {"tool_name": "request_user_input"}}
                self.assertFalse(publish_input_notice(self.inbox, payload, now=self.now))
        self.assertFalse(self.inbox.exists())

    def test_content_fields_are_never_inspected(self):
        class MetadataOnlyPayload(dict):
            def get(self, key, *arguments):
                if key in {"tool_input", "prompt", "tool_response", "last_assistant_message", "transcript_path", "token"}:
                    raise AssertionError("Content fields must not be read")
                return super().get(key, *arguments)

        self.assertTrue(publish_input_notice(self.inbox, MetadataOnlyPayload(self.payload()), now=self.now))
        path, record = self.record()
        saved = path.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE", saved)
        self.assertNotIn("PrivateWorkspace", saved)
        self.assertNotIn("tool_input", saved)
        self.assertNotIn("transcript", saved)
        self.assertEqual(set(record), notices._FIELDS)
        row, = load_input_notices(self.inbox, self.now)
        self.assertEqual(row.project, "Synthetic Project")
        self.assertEqual((row.task_id, row.turn_id), ("synthetic-session", "synthetic-turn"))
        self.assertEqual(len(row.notice_id), 64)
        self.assertLess(path.stat().st_size, notices.MAX_NOTICE_BYTES)

    def test_duplicate_identity_preserves_original_observation_and_file(self):
        self.assertTrue(self.publish())
        path, record = self.record()
        original = path.read_bytes(), path.stat().st_mtime_ns
        self.assertTrue(self.publish(now=self.now + timedelta(minutes=1)))
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), original)
        row, = load_input_notices(self.inbox, self.now + timedelta(minutes=1))
        self.assertEqual(row.observed_at, record["observed_at"])
        self.assertEqual(len(self.files()), 1)

    def test_distinct_questions_and_statuses_are_not_merged_even_at_same_time(self):
        self.assertTrue(self.publish(question="question-a"))
        self.assertTrue(self.publish(question="question-b"))
        self.assertTrue(self.publish("request_user_input_async", question="question-a"))
        self.assertTrue(self.publish(question="question-a", turn="another-turn"))
        rows = load_input_notices(self.inbox, self.now)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({row.notice_id for row in rows}), 4)

    def test_notices_are_newest_first_without_latest_turn_coalescing(self):
        self.assertTrue(self.publish(question="old"))
        self.assertTrue(self.publish(question="middle", turn="another-turn", now=self.now + timedelta(seconds=1)))
        self.assertTrue(self.publish(question="new", now=self.now + timedelta(seconds=2)))
        rows = load_input_notices(self.inbox, self.now + timedelta(seconds=2))
        self.assertEqual(len(rows), 3)
        self.assertEqual([row.observed_at for row in rows], sorted((row.observed_at for row in rows), reverse=True))

    def test_fast_running_and_terminal_snapshots_cannot_erase_question_notices(self):
        self.assertTrue(self.publish("request_user_input_async"))
        original = self.files()[0].read_bytes()
        for index, event in enumerate(("PreToolUse", "Stop"), 1):
            payload = self.payload() | {"hook_event_name": event, "tool_name": "Bash"}
            self.assertTrue(publish_hook_event(self.inbox, payload, now=self.now + timedelta(seconds=index)))
        row, = load_input_notices(self.inbox, self.now + timedelta(seconds=2))
        self.assertEqual(row.status, "question_opened")
        self.assertEqual(self.files()[0].read_bytes(), original)
        snapshot, = load_snapshots(self.inbox, self.now + timedelta(seconds=2))
        self.assertEqual(snapshot.status, "turn_ended")

    def test_reading_does_not_create_directories_or_lock_files(self):
        self.assertEqual(load_input_notices(self.inbox, self.now), ())
        self.assertFalse(self.inbox.exists())
        self.inbox.mkdir()
        self.assertEqual(load_input_notices(self.inbox, self.now), ())
        self.assertEqual(list(self.inbox.iterdir()), [])
        self.directory.mkdir()
        self.assertEqual(load_input_notices(self.inbox, self.now), ())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_stale_notice_is_not_proof_of_failure_or_unanswered_question(self):
        self.assertTrue(self.publish())
        row, = load_input_notices(self.inbox, self.now)
        self.assertFalse(row.is_stale(self.now + timedelta(seconds=599)))
        self.assertTrue(row.is_stale(self.now + timedelta(minutes=10)))
        self.assertEqual(load_input_notices(self.inbox, self.now + timedelta(days=1))[0].status, "input_requested")
        self.assertEqual(len(self.files()), 1)

    def test_missing_invalid_ids_events_and_naive_time_do_not_create_inbox(self):
        for field in ("session_id", "turn_id", "tool_use_id"):
            for invalid in (None, "", "../private", "a/b", "有名字", "x" * 129, 12):
                with self.subTest(field=field, invalid=invalid):
                    self.assertFalse(publish_input_notice(self.inbox, self.payload() | {field: invalid}, now=self.now))
            payload = self.payload()
            payload.pop(field)
            self.assertFalse(publish_input_notice(self.inbox, payload, now=self.now))
        for change in ({"hook_event_name": "PostToolUse"}, {"hook_event_name": "PermissionRequest"},
                       {"hook_event_name": None}, {"cwd": "C:\\private\\.."}):
            self.assertFalse(publish_input_notice(self.inbox, self.payload() | change, now=self.now))
        self.assertFalse(publish_input_notice(self.inbox, [], now=self.now))
        self.assertFalse(publish_input_notice(self.inbox, self.payload(), now=self.now.replace(tzinfo=None)))
        self.assertFalse(self.inbox.exists())

    def test_safe_maximum_ids_and_missing_project_have_bounded_metadata(self):
        payload = self.payload() | {"session_id": "s" * 128, "turn_id": "t" * 128, "tool_use_id": "q" * 128}
        payload.pop("cwd")
        self.assertTrue(publish_input_notice(self.inbox, payload, now=self.now))
        row, = load_input_notices(self.inbox, self.now)
        self.assertEqual(row.project, "未命名项目")

    def test_strict_schema_future_time_and_digest_tampering_are_rejected(self):
        self.assertTrue(self.publish())
        path, original = self.record()
        changes = ({"producer": "other"}, {"source": "grok_bot"}, {"schema_version": True},
                   {"notice_id": "0" * 64}, {"session_id": "other-session"}, {"tool_use_id": "bad/id"},
                   {"status": "paused"}, {"project_basename": "C:/private"}, {"project_basename": "."},
                   {"project_basename": "\x00"}, {"observed_at": "2026-10-25T00:00:00"},
                   {"observed_at": "2026-10-25T00:00:00.000001Z"}, {"prompt": "PRIVATE"})
        for change in changes:
            with self.subTest(change=change):
                path.write_text(json.dumps(original | change), encoding="utf-8")
                self.assertEqual(load_input_notices(self.inbox, self.now), ())
        for field in original:
            with self.subTest(missing=field):
                changed = dict(original)
                changed.pop(field)
                path.write_text(json.dumps(changed), encoding="utf-8")
                self.assertEqual(load_input_notices(self.inbox, self.now), ())

    def test_wrong_filename_illegal_encoding_and_duplicate_fields_are_rejected(self):
        self.assertTrue(self.publish())
        path, record = self.record()
        raw = json.dumps(record)
        for corrupt in (b"\xff", raw.encode("utf-16"), b"not-json", b"[]", b" " * notices.MAX_NOTICE_BYTES,
                        (raw[:-1] + ',"schema_version":1}').encode("utf-8")):
            path.write_bytes(corrupt)
            self.assertEqual(load_input_notices(self.inbox, self.now), ())
        path.write_text(raw, encoding="utf-8")
        path.rename(self.directory / (notices.PREFIX + "0" * 64 + ".json"))
        self.assertEqual(load_input_notices(self.inbox, self.now), ())

    def test_corrupt_same_identity_is_preserved_instead_of_overwritten(self):
        self.assertTrue(self.publish())
        path, _ = self.record()
        path.write_bytes(b"user-owned damaged content")
        self.assertFalse(self.publish(now=self.now + timedelta(seconds=1)))
        self.assertEqual(path.read_bytes(), b"user-owned damaged content")

    def test_fresh_capacity_rejects_new_notice_and_preserves_unrelated_files(self):
        for index in range(notices.MAX_NOTICES):
            self.assertTrue(self.publish(question=f"question-{index}"))
        unrelated = self.directory / "user-extension.json"
        unrelated.write_text("preserve user file", encoding="utf-8")
        corrupt = self.directory / (notices.PREFIX + "0" * 64 + ".json")
        corrupt.write_text("not our valid record", encoding="utf-8")
        self.assertFalse(self.publish(question="new-question", now=self.now + timedelta(seconds=1)))
        self.assertTrue(self.publish(question="question-0", now=self.now + timedelta(seconds=1)))
        self.assertEqual(len(load_input_notices(self.inbox, self.now + timedelta(seconds=1))), notices.MAX_NOTICES)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve user file")
        self.assertEqual(corrupt.read_text(encoding="utf-8"), "not our valid record")

    def test_at_capacity_only_valid_expired_notices_are_evicted(self):
        for index in range(notices.MAX_NOTICES):
            self.assertTrue(self.publish(question=f"question-{index}"))
        corrupt = self.directory / (notices.PREFIX + "0" * 64 + ".json")
        corrupt.write_text("preserve damaged record", encoding="utf-8")
        original = {path.name for path in self.files() if path != corrupt}
        later = self.now + timedelta(minutes=10)
        self.assertTrue(self.publish(question="new-question", now=later))
        rows = load_input_notices(self.inbox, later)
        self.assertEqual(len(rows), notices.MAX_NOTICES)
        self.assertEqual(sum(not row.is_stale(later) for row in rows), 1)
        current = {path.name for path in self.files() if path != corrupt}
        self.assertEqual(len(original - current), 1)
        self.assertEqual(corrupt.read_text(encoding="utf-8"), "preserve damaged record")

    def test_directory_scan_is_bounded_without_deleting_unrelated_entries(self):
        self.assertTrue(self.publish())
        for index in range(4):
            (self.directory / f"unrelated-{index}.txt").write_text("keep", encoding="utf-8")
        with patch.object(notices, "MAX_DIRECTORY_ENTRIES", 4):
            self.assertEqual(load_input_notices(self.inbox, self.now), ())
            self.assertFalse(self.publish(question="new-question"))
        self.assertEqual(len(load_input_notices(self.inbox, self.now)), 1)
        self.assertEqual(len(list(self.directory.glob("unrelated-*.txt"))), 4)
        self.assertEqual(notices.MAX_DIRECTORY_ENTRIES, 2048)

    def test_reading_is_bounded_even_when_more_valid_records_exist(self):
        for index in range(3):
            self.assertTrue(self.publish(question=f"question-{index}"))
        with patch.object(notices, "MAX_NOTICES", 2):
            self.assertEqual(len(load_input_notices(self.inbox, self.now)), 2)
            self.assertFalse(self.publish(question="extra", now=self.now + timedelta(minutes=11)))
        self.assertEqual(len(self.files()), 3)

    def test_relative_traversal_and_linked_paths_are_refused(self):
        relative = Path("relative-input-fixture")
        traversal = self.root / "unused" / ".." / "events"
        for inbox in (relative, traversal):
            self.assertFalse(publish_input_notice(inbox, self.payload(), now=self.now))
            self.assertEqual(load_input_notices(inbox, self.now), ())
        for linked in (self.inbox, self.root, self.directory):
            with self.subTest(linked=linked), patch.object(Path, "is_symlink", autospec=True,
                                                         side_effect=lambda path: path == linked):
                self.assertFalse(self.publish())
                self.assertEqual(load_input_notices(self.inbox, self.now), ())
        self.assertFalse(self.directory.exists())

    def test_linked_record_is_not_loaded_or_replaced(self):
        self.assertTrue(self.publish())
        path, _ = self.record()
        original = path.read_bytes()
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda candidate: candidate == path):
            self.assertEqual(load_input_notices(self.inbox, self.now), ())
            self.assertFalse(self.publish(now=self.now + timedelta(seconds=1)))
        self.assertEqual(path.read_bytes(), original)

    def test_file_instead_of_inbox_or_notice_directory_is_untouched(self):
        self.inbox.write_text("user file", encoding="utf-8")
        self.assertFalse(self.publish())
        self.assertEqual(load_input_notices(self.inbox, self.now), ())
        self.assertEqual(self.inbox.read_text(encoding="utf-8"), "user file")
        self.inbox.unlink()
        self.inbox.mkdir()
        self.directory.write_text("another user file", encoding="utf-8")
        self.assertFalse(self.publish())
        self.assertEqual(self.directory.read_text(encoding="utf-8"), "another user file")

    def test_nonblocking_lock_collision_does_not_lose_existing_notices(self):
        self.assertTrue(self.publish())
        with notices._inbox_lock(self.directory.resolve()) as locked:
            self.assertTrue(locked)
            self.assertFalse(self.publish(question="new-question"))
        self.assertTrue(self.publish(question="new-question"))
        self.assertEqual(len(load_input_notices(self.inbox, self.now)), 2)

    def test_lock_failure_and_atomic_write_failure_are_quiet_and_clean(self):
        with patch.object(notices, "_inbox_lock", return_value=nullcontext(False)):
            self.assertFalse(self.publish())
        self.assertEqual(self.files(), [])
        with patch.object(notices.os, "replace", side_effect=PermissionError("synthetic failure")):
            self.assertFalse(self.publish())
        self.assertEqual(self.files(), [])
        self.assertEqual(list(self.directory.glob("*.tmp")), [])
        self.assertTrue(self.publish())


if __name__ == "__main__":
    unittest.main()
