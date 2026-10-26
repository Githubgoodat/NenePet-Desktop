"""Synthetic heartbeat files only; no production channel or account access."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aipet.core.workflow_question_status import (
    MAX_STATUS_BYTES, STATUS_FILE, read_channel_status, write_channel_status,
)


class ChannelStatusTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="question-channel-fixture-"))).resolve()
        self.now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        self.path = self.root / STATUS_FILE

    def write(self, enabled=True, accepting=True, now=None):
        write_channel_status(self.root, enabled=enabled, accepting=accepting, now=now or self.now)

    def test_missing_root_and_status_are_offline_without_creating_directories(self):
        missing = self.root / "missing"
        self.assertEqual(read_channel_status(missing, self.now), "offline")
        write_channel_status(missing, enabled=False, accepting=False, now=self.now)
        self.assertFalse(missing.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_ready_paused_disabled_and_metadata_only_schema(self):
        for enabled, accepting, expected in ((True, True, "ready"), (True, False, "paused"),
                                               (False, True, "disabled")):
            with self.subTest(expected=expected):
                self.write(enabled, accepting)
                self.assertEqual(read_channel_status(self.root, self.now), expected)
        value = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(value), {"schema_version", "producer", "enabled", "accepting", "observed_at"})
        self.assertFalse(value["accepting"])
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_age_limit_is_inclusive_and_future_stale_or_naive_clocks_fail_closed(self):
        self.write()
        self.assertEqual(read_channel_status(self.root, self.now + timedelta(seconds=20)), "ready")
        self.assertEqual(read_channel_status(self.root, self.now + timedelta(seconds=20, microseconds=1)), "offline")
        self.assertEqual(read_channel_status(self.root, self.now - timedelta(microseconds=1)), "offline")
        self.assertEqual(read_channel_status(self.root, self.now.replace(tzinfo=None)), "offline")

    def test_wrong_fields_types_duplicate_keys_and_noncanonical_times_fail_closed(self):
        self.write()
        original = json.loads(self.path.read_text(encoding="utf-8"))
        cases = [original | {"enabled": 1}, original | {"schema_version": True},
                 original | {"private_body": "never accepted"},
                 original | {"producer": "another-owner"},
                 original | {"observed_at": self.now.isoformat()}]
        for value in cases:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                self.assertEqual(read_channel_status(self.root, self.now), "offline")
        self.path.write_text(json.dumps(original)[:-1] + ',"enabled":false}', encoding="utf-8")
        self.assertEqual(read_channel_status(self.root, self.now), "offline")

    def test_corrupt_oversized_directory_and_unknown_files_are_not_overwritten(self):
        for raw in (b"user-owned file", b"{" + b" " * MAX_STATUS_BYTES):
            self.path.write_bytes(raw)
            self.assertEqual(read_channel_status(self.root, self.now), "offline")
            self.write()
            self.assertEqual(self.path.read_bytes(), raw)
        self.path.unlink()
        self.path.mkdir()
        self.write()
        self.assertTrue(self.path.is_dir())
        self.assertEqual(read_channel_status(self.root, self.now), "offline")

    def test_relative_parent_traversal_and_reparse_ancestor_are_rejected(self):
        self.write()
        self.assertEqual(read_channel_status(Path("relative-inbox"), self.now), "offline")
        self.assertEqual(read_channel_status(Path(self.root.anchor), self.now), "offline")
        self.assertEqual(read_channel_status(self.root / "child" / "..", self.now), "offline")
        original = Path.lstat

        def linked(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            return (SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                    if path == self.root else info)

        before = self.path.read_bytes()
        with patch.object(Path, "lstat", linked):
            self.assertEqual(read_channel_status(self.root, self.now), "offline")
            self.write(False, False)
        self.assertEqual(self.path.read_bytes(), before)

    def test_hardlinked_status_is_neither_read_nor_replaced(self):
        self.write()
        other = self.root / "other-file"
        try:
            os.link(self.path, other)
        except OSError:
            self.skipTest("Hard links unavailable in this synthetic directory")
        before = self.path.read_bytes()
        self.assertEqual(read_channel_status(self.root, self.now), "offline")
        self.write(False, False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(other.read_bytes(), before)

    def test_parallel_refreshes_leave_one_complete_document_and_no_temporary_files(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda index: self.write(True, bool(index % 2)), range(12)))
        self.assertIn(read_channel_status(self.root, self.now), {"ready", "paused"})
        self.assertEqual(list(self.root.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
