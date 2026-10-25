"""Synthetic atomic persistence failures; no real profile or memory access."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from aipet.core import storage
from aipet.core.storage import HistoryStore, ScreenMemoryEntry, ScreenMemoryStore


def windows_error(code: int) -> OSError:
    error = OSError(errno.EACCES, "synthetic Windows replacement conflict")
    error.winerror = code
    return error


class AtomicStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="nene-storage-atomic-")))
        self.path = self.root / "history.json"
        self.previous = {"messages": [{"role": "user", "content": "synthetic previous record"}]}
        self.path.write_text(json.dumps(self.previous), encoding="utf-8")

    def temporary_files(self):
        return list(self.root.glob(".aipet-storage-*.tmp"))

    def test_transient_windows_access_and_sharing_conflicts_recover(self):
        actual_replace = Path.replace
        for code in (5, 32, 33):
            with self.subTest(code=code):
                calls = []
                conflict = windows_error(code)
                def replace_after_conflicts(source, destination):
                    calls.append(source)
                    if len(calls) < 3:
                        raise conflict
                    return actual_replace(source, destination)
                with patch.object(storage, "_WINDOWS_REPLACE_RETRY", True), \
                        patch.object(storage.time, "sleep") as sleep, \
                        patch.object(Path, "replace", autospec=True, side_effect=replace_after_conflicts):
                    HistoryStore(self.path).save([{"role": "user", "content": f"new record {code}"}])
                self.assertEqual(len(calls), 3)
                self.assertEqual(len(set(calls)), 1)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.02])
                self.assertIn(str(code), json.loads(self.path.read_text(encoding="utf-8"))["messages"][0]["content"])
                self.assertEqual(self.temporary_files(), [])

    def test_permanent_denial_exhausts_bounded_retries_and_preserves_original_error_and_file(self):
        original = self.path.read_bytes()
        error = windows_error(5)
        with patch.object(storage, "_WINDOWS_REPLACE_RETRY", True), \
                patch.object(storage.time, "monotonic", return_value=0), \
                patch.object(storage.time, "sleep") as sleep, \
                patch.object(Path, "replace", side_effect=error) as replace:
            with self.assertRaises(OSError) as caught:
                HistoryStore(self.path).save([])
        self.assertIs(caught.exception, error)
        self.assertEqual(replace.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], list(storage._REPLACE_RETRY_DELAYS))
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 0.5)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.temporary_files(), [])

    def test_deadline_exhaustion_raises_without_sleeping_past_budget(self):
        error = windows_error(32)
        with patch.object(storage, "_WINDOWS_REPLACE_RETRY", True), \
                patch.object(storage.time, "monotonic", side_effect=[0, 0.499]), \
                patch.object(storage.time, "sleep") as sleep, \
                patch.object(Path, "replace", side_effect=error) as replace:
            with self.assertRaises(OSError) as caught:
                HistoryStore(self.path).save([])
        self.assertIs(caught.exception, error)
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.previous)

    def test_nonsharing_errors_and_nonwindows_permission_errors_do_not_retry(self):
        for error, is_windows in ((OSError(errno.ENOSPC, "synthetic disk full"), True),
                                  (windows_error(2), True), (PermissionError("no Windows code"), True),
                                  (windows_error(5), False)):
            with self.subTest(winerror=getattr(error, "winerror", None), windows=is_windows), \
                    patch.object(storage, "_WINDOWS_REPLACE_RETRY", is_windows), \
                    patch.object(storage.time, "sleep") as sleep, \
                    patch.object(Path, "replace", side_effect=error) as replace:
                with self.assertRaises(OSError) as caught:
                    HistoryStore(self.path).save([])
                self.assertIs(caught.exception, error)
                self.assertEqual(replace.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.previous)
                self.assertEqual(self.temporary_files(), [])

    def test_unique_same_directory_temporary_is_synced_and_closed_before_replace(self):
        actual_factory = storage.tempfile.NamedTemporaryFile
        actual_replace = Path.replace
        streams, sources = [], []
        @contextmanager
        def track_stream(*arguments, **keywords):
            self.assertEqual(Path(keywords["dir"]), self.root)
            with actual_factory(*arguments, **keywords) as stream:
                streams.append(stream)
                yield stream
        def inspect_replace(source, destination):
            sources.append(source)
            self.assertTrue(streams[-1].closed)
            self.assertEqual(source.parent, self.path.parent)
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(json.loads(source.read_text(encoding="utf-8")), {"messages": []})
            return actual_replace(source, destination)
        with patch.object(storage.tempfile, "NamedTemporaryFile", side_effect=track_stream), \
                patch.object(storage.os, "fsync", wraps=storage.os.fsync) as fsync, \
                patch.object(Path, "replace", autospec=True, side_effect=inspect_replace):
            HistoryStore(self.path).save([])
            HistoryStore(self.path).save([])
        self.assertEqual(fsync.call_count, 2)
        self.assertEqual(len(set(sources)), 2)
        self.assertEqual(self.temporary_files(), [])

    def test_fixed_legacy_temporary_and_unrelated_files_are_untouched(self):
        legacy = self.path.with_suffix(".json.tmp")
        legacy.write_text("user-owned legacy temporary", encoding="utf-8")
        unrelated = self.root / ".aipet-storage-unrelated.tmp"
        unrelated.write_text("another writer's temporary", encoding="utf-8")
        error = OSError("synthetic replacement failure")
        with patch.object(Path, "replace", side_effect=error):
            with self.assertRaises(OSError):
                HistoryStore(self.path).save([])
        self.assertEqual(legacy.read_text(encoding="utf-8"), "user-owned legacy temporary")
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "another writer's temporary")
        self.assertEqual(self.temporary_files(), [unrelated])

    def test_serialization_and_encoding_failure_do_not_touch_old_file_or_create_parent(self):
        original = self.path.read_bytes()
        for content in (object(), "invalid\ud800"):
            with self.subTest(content_type=type(content).__name__), \
                    patch.object(storage.tempfile, "NamedTemporaryFile") as factory:
                with self.assertRaises((TypeError, UnicodeError)):
                    HistoryStore(self.path).save([{"role": "user", "content": content}])
                factory.assert_not_called()
                self.assertEqual(self.path.read_bytes(), original)
        missing = self.root / "missing" / "history.json"
        with self.assertRaises(TypeError):
            HistoryStore(missing).save([{"role": "user", "content": object()}])
        self.assertFalse(missing.parent.exists())

    def test_flush_or_sync_failure_keeps_old_target_and_cleans_only_owned_temporary(self):
        original = self.path.read_bytes()
        error = OSError("synthetic sync failure")
        with patch.object(storage.os, "fsync", side_effect=error), \
                patch.object(Path, "replace") as replace:
            with self.assertRaises(OSError) as caught:
                HistoryStore(self.path).save([])
        self.assertIs(caught.exception, error)
        replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.temporary_files(), [])

    def test_cleanup_failure_cannot_hide_original_save_error(self):
        original = OSError("synthetic save failure")
        actual_unlink = Path.unlink
        failed_cleanup = []
        def refuse_own_cleanup(path, *arguments, **keywords):
            if path.name.startswith(".aipet-storage-"):
                failed_cleanup.append(path)
                raise PermissionError("synthetic cleanup failure")
            return actual_unlink(path, *arguments, **keywords)
        with patch.object(Path, "replace", side_effect=original), \
                patch.object(Path, "unlink", autospec=True, side_effect=refuse_own_cleanup):
            with self.assertRaises(OSError) as caught:
                HistoryStore(self.path).save([])
        self.assertIs(caught.exception, original)
        self.assertEqual(len(failed_cleanup), 1)
        self.assertEqual(failed_cleanup[0].parent, self.root)
        failed_cleanup[0].unlink()

    def test_concurrent_writers_have_distinct_complete_temporaries_and_a_complete_final_record(self):
        actual_replace = Path.replace
        barrier = threading.Barrier(2, timeout=5)
        sources = []
        def coordinated_replace(source, destination):
            sources.append(source)
            self.assertEqual(len(json.loads(source.read_text(encoding="utf-8"))["messages"]), 1)
            barrier.wait()
            return actual_replace(source, destination)
        # The synchronization hook is visited once per writer; actual Windows
        # contention, if any, is handled by the helper on later attempts.
        def first_replace_per_writer(source, destination):
            if source not in sources:
                return coordinated_replace(source, destination)
            return actual_replace(source, destination)
        with patch.object(Path, "replace", autospec=True, side_effect=first_replace_per_writer):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(HistoryStore(self.path).save,
                                      [{"role": "user", "content": f"writer-{index}"}]) for index in range(2)]
                for future in futures:
                    future.result(timeout=5)
        self.assertEqual(len(set(sources)), 2)
        self.assertIn(json.loads(self.path.read_text(encoding="utf-8"))["messages"][0]["content"], {"writer-0", "writer-1"})
        self.assertEqual(self.temporary_files(), [])

    def test_history_limit_and_existing_proactive_fields_are_unchanged(self):
        store = HistoryStore(self.path, limit=4, separate_proactive=True)
        store.record_proactive("synthetic pending reply", "synthetic event context")
        pending = dict(store._pending_proactive)
        messages = [{"role": "user", "content": f"message-{index}"} for index in range(7)]
        store.save(messages)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"messages": messages[-4:], "nene_pending_proactive": pending})
        restored = HistoryStore(self.path, limit=4, separate_proactive=True)
        self.assertEqual(restored.load(), messages[-4:])
        self.assertEqual(restored._pending_proactive, pending)

    def test_screen_store_uses_same_atomic_path_without_schema_changes(self):
        path = self.root / "screen.json"
        store = ScreenMemoryStore(path=path, limit=2)
        entry = ScreenMemoryEntry(occurred_at="2026-10-25T12:00:00+09:00", change_summary="synthetic screen change")
        store.entries = [entry]
        actual_replace = Path.replace
        attempts = []
        def once_conflicted(source, destination):
            attempts.append(source)
            if len(attempts) == 1:
                raise windows_error(33)
            return actual_replace(source, destination)
        with patch.object(storage, "_WINDOWS_REPLACE_RETRY", True), \
                patch.object(storage.time, "sleep") as sleep, \
                patch.object(Path, "replace", autospec=True, side_effect=once_conflicted):
            store.save()
        sleep.assert_called_once_with(0.01)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"events": [entry.model_dump(mode="json")]})
        self.assertEqual(ScreenMemoryStore(path=path).entries, [entry])
        self.assertEqual(self.temporary_files(), [])


if __name__ == "__main__":
    unittest.main()
