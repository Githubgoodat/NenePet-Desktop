"""Synthetic cooperation questions only; no accounts, memory, network or DPAPI."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aipet.core import workflow_questions as questions
from aipet.core.workflow_questions import QuestionStore, QuestionStoreError


def protect(value):
    # Reversible opaque test codec; production always uses the credential facade.
    return "synthetic:" + base64.b64encode(bytes(byte ^ 91 for byte in value.encode("utf-8"))).decode("ascii")


def unprotect(value):
    if not value.startswith("synthetic:"):
        raise ValueError("SYNTHETIC PRIVATE ENCRYPTION ERROR")
    return bytes(byte ^ 91 for byte in base64.b64decode(value[len("synthetic:"):], validate=True)).decode("utf-8")


class WorkflowQuestionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="nene-question-test-")))
        self.root = self.temporary / "questions"
        self.now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        self.store = self.make_store()
        self.binding = {"source": "codex", "task_id": "synthetic-task", "turn_id": "synthetic-turn"}

    def make_store(self, **dependencies):
        return QuestionStore(self.root, clock=lambda: self.now,
                             protect=dependencies.get("protect", protect),
                             unprotect=dependencies.get("unprotect", unprotect))

    def create(self, **changes):
        values = self.binding | {"project": "Synthetic Project", "question": "SYNTHETIC PRIVATE QUESTION",
                                 "options": ("First choice", "Second choice"), "timeout_seconds": 60}
        return self.store.create(**(values | changes))

    def get(self, record, **changes):
        return self.store.get(record.request_id, **(self.binding | changes))

    def answer(self, record, **changes):
        return self.store.submit_reply(**({"code": record.code, "answer": "SYNTHETIC PRIVATE ANSWER",
            "message_id": "synthetic-message", "owner_key": "synthetic-owner"} | changes))

    def path(self, record):
        return self.root / (questions.PREFIX + record.request_id + ".json")

    def state(self, record):
        return json.loads(unprotect(json.loads(self.path(record).read_text("utf-8"))["encrypted_state"]))

    def replace_state(self, record, **changes):
        value = self.state(record) | changes
        self.path(record).write_text(json.dumps({"format": questions.FORMAT,
            "encrypted_state": protect(json.dumps(value, ensure_ascii=False))}), encoding="utf-8")

    def test_absent_store_reads_create_nothing(self):
        self.assertFalse(self.root.exists())
        self.assertEqual(self.store.list_pending(), ())
        self.assertIsNone(self.store.get("0" * 32, **self.binding))
        self.assertFalse(self.store.claim_delivery("0" * 32, "synthetic-owner"))
        self.assertEqual(self.store.submit_reply(code="00000000", answer="x", message_id="id", owner_key="owner"),
                         "not_found")
        self.assertFalse(self.store.cancel("0" * 32, **self.binding))
        self.assertFalse(self.root.exists())

    def test_frozen_record_and_envelope_expose_no_content_in_repr_or_disk(self):
        record = self.create()
        self.assertRegex(record.request_id, r"^[0-9a-f]{32}$")
        self.assertRegex(record.code, r"^[0-9A-F]{8}$")
        self.assertEqual(record.options, ("First choice", "Second choice"))
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.created_at, "2026-10-25T00:00:00.000000Z")
        self.assertEqual(record.expires_at, "2026-10-25T00:01:00.000000Z")
        self.assertEqual(self.store.list_pending(), (record,))
        self.assertEqual(self.get(record), record)
        with self.assertRaises(FrozenInstanceError):
            record.answer = "changed"
        self.assertNotIn(record.question, repr(record))
        raw = self.path(record).read_text("utf-8")
        self.assertEqual(set(json.loads(raw)), {"format", "encrypted_state"})
        for private in (record.question, record.project, record.task_id, record.turn_id, record.code, "First choice"):
            self.assertNotIn(private, raw)

    def test_get_is_strictly_bound_to_source_task_and_turn(self):
        record = self.create()
        for change in ({"source": "grok_bot"}, {"task_id": "other-task"}, {"turn_id": "other-turn"}):
            self.assertIsNone(self.get(record, **change))
            self.assertFalse(self.store.cancel(record.request_id, **(self.binding | change)))
        for identity in ("../outside", "0" * 33, "NOT-AN-ID", [], None):
            self.assertIsNone(self.store.get(identity, **self.binding))
        self.assertEqual(self.get(record).status, "pending")

    def test_both_sources_and_distinct_questions_remain_independent(self):
        first, second = self.create(), self.create(source="grok_bot")
        self.assertNotEqual(first.request_id, second.request_id)
        self.assertNotEqual(first.code, second.code)
        self.assertEqual(len(self.store.list_pending()), 2)
        self.assertIsNone(self.get(second))
        self.assertEqual(self.get(second, source="grok_bot"), second)

    def test_claim_is_durable_before_delivery_and_never_reclaims(self):
        record = self.create()
        self.assertEqual(self.answer(record), "not_delivered")
        self.assertTrue(self.store.claim_delivery(record.request_id, "synthetic-owner"))
        self.assertTrue(self.get(record).delivery_attempted)
        self.assertEqual(self.get(record).owner_key, "synthetic-owner")
        original = self.path(record).read_bytes()
        fresh = self.make_store()
        self.assertFalse(fresh.claim_delivery(record.request_id, "synthetic-owner"))
        self.assertFalse(fresh.claim_delivery(record.request_id, "another-owner"))
        self.assertEqual(self.path(record).read_bytes(), original)

    def test_first_valid_answer_is_locked_and_collection_is_idempotent(self):
        record = self.create()
        self.assertTrue(self.store.claim_delivery(record.request_id, "synthetic-owner"))
        self.assertEqual(self.answer(record, owner_key="another-owner"), "owner_mismatch")
        self.assertEqual(self.answer(record), "accepted")
        original = self.path(record).read_bytes()
        self.assertEqual(self.answer(record), "duplicate_message")
        self.assertEqual(self.answer(record, message_id="new-message", answer="replacement"), "already_answered")
        self.assertEqual(self.path(record).read_bytes(), original)
        for _ in range(3):
            loaded = self.make_store().get(record.request_id, **self.binding)
            self.assertEqual((loaded.status, loaded.answer), ("answered", "SYNTHETIC PRIVATE ANSWER"))
        self.assertEqual(self.store.list_pending(), ())
        self.assertFalse(self.store.cancel(record.request_id, **self.binding))
        self.assertNotIn("SYNTHETIC PRIVATE ANSWER", self.path(record).read_text("utf-8"))

    def test_same_owner_message_id_cannot_answer_a_different_question(self):
        first, second = self.create(), self.create()
        for record in (first, second):
            self.assertTrue(self.store.claim_delivery(record.request_id, "synthetic-owner"))
        self.assertEqual(self.answer(first), "accepted")
        self.assertEqual(self.answer(second), "duplicate_message")
        self.assertEqual(self.get(second).status, "pending")
        self.assertEqual(self.answer(second, message_id="another-message"), "accepted")
        self.assertNotIn("synthetic-message", self.path(first).read_text("utf-8"))

    def test_options_map_ascii_letter_or_number_and_free_text_stays_data(self):
        for answer, expected in (("A", "First choice"), ("b", "Second choice"), ("1", "First choice"),
                                 ("2", "Second choice"), ("C", "C"), ("7", "7"),
                                 ("powershell -Command should-not-run", "powershell -Command should-not-run")):
            with self.subTest(answer=answer):
                record = self.create()
                self.assertTrue(self.store.claim_delivery(record.request_id, "synthetic-owner"))
                self.assertEqual(self.answer(record, answer=answer, message_id="message-" + record.code), "accepted")
                self.assertEqual(self.get(record).answer, expected)

    def test_expiry_from_creation_applies_to_pending_answered_and_late_collection(self):
        pending, answered = self.create(), self.create()
        for record in (pending, answered):
            self.assertTrue(self.store.claim_delivery(record.request_id, "synthetic-owner"))
        self.assertEqual(self.answer(answered), "accepted")
        self.now += timedelta(seconds=60)
        self.assertEqual(self.store.list_pending(), ())
        for record in (pending, answered):
            loaded = self.get(record)
            self.assertEqual((loaded.status, loaded.answer), ("expired", ""))
            self.assertEqual(self.answer(record, message_id="late"), "expired")
            self.assertFalse(self.store.claim_delivery(record.request_id, "synthetic-owner"))
            self.assertFalse(self.store.cancel(record.request_id, **self.binding))

    def test_slow_decryption_cannot_return_an_answer_after_expiry(self):
        record = self.create()
        self.store.claim_delivery(record.request_id, "synthetic-owner")
        self.assertEqual(self.answer(record), "accepted")
        def slow_unprotect(value):
            decoded = unprotect(value)
            self.now += timedelta(seconds=61)
            return decoded
        store = self.make_store(unprotect=slow_unprotect)
        loaded = store.get(record.request_id, **self.binding)
        self.assertEqual((loaded.status, loaded.answer), ("expired", ""))

    def test_expiry_during_encryption_rejects_commit_and_keeps_pending_file(self):
        record = self.create()
        self.store.claim_delivery(record.request_id, "synthetic-owner")
        original = self.path(record).read_bytes()
        def slow_protect(value):
            self.now += timedelta(seconds=61)
            return protect(value)
        store = self.make_store(protect=slow_protect)
        self.assertEqual(store.submit_reply(code=record.code, answer="late", message_id="late", owner_key="synthetic-owner"),
                         "expired")
        self.assertEqual(self.path(record).read_bytes(), original)
        self.assertEqual(self.get(record).answer, "")
        self.assertFalse(list(self.root.glob(".nene-question-*.tmp")))

    def test_cancel_is_bound_durable_and_idempotent_without_reopening(self):
        record = self.create()
        self.assertTrue(self.store.cancel(record.request_id, **self.binding))
        original = self.path(record).read_bytes()
        self.assertTrue(self.store.cancel(record.request_id, **self.binding))
        self.assertEqual(self.path(record).read_bytes(), original)
        self.assertEqual(self.get(record).status, "cancelled")
        self.assertFalse(self.store.claim_delivery(record.request_id, "synthetic-owner"))
        self.assertEqual(self.answer(record), "cancelled")
        self.assertEqual(self.store.list_pending(), ())

    def test_invalid_creation_is_bounded_and_touches_no_directory(self):
        changes = ({"source": "other"}, {"task_id": "../escape"}, {"turn_id": ""},
                   {"project": "C:\\private\\project"}, {"project": ".."}, {"project": "x" * 97},
                   {"question": ""}, {"question": "x" * 801}, {"question": "bad\x00field"},
                   {"question": "\ud800"}, {"options": ("x",) * 7}, {"options": ("x" * 121,)},
                   {"options": ("",)}, {"options": "unbounded iterable"},
                   {"timeout_seconds": 59}, {"timeout_seconds": 901}, {"timeout_seconds": True},
                   {"timeout_seconds": 60.0})
        for change in changes:
            with self.subTest(change=repr(change)):
                with self.assertRaises(QuestionStoreError) as raised:
                    self.create(**change)
                self.assertEqual(raised.exception.code, "invalid_request")
                self.assertIsNone(raised.exception.__context__)
                self.assertFalse(self.root.exists())

    def test_invalid_reply_and_unknown_code_never_mutate_question(self):
        record = self.create()
        self.store.claim_delivery(record.request_id, "synthetic-owner")
        original = self.path(record).read_bytes()
        for change in ({"answer": " "}, {"answer": "x" * 2001}, {"answer": "bad\x00field"},
                       {"answer": "\ud800"}, {"message_id": ""}, {"message_id": 123},
                       {"owner_key": ""}, {"code": "../escape"}, {"code": None}):
            with self.subTest(change=repr(change)):
                self.assertEqual(self.answer(record, **change), "invalid_reply")
        unknown = "FFFFFFFF" if record.code != "FFFFFFFF" else "EEEEEEEE"
        self.assertEqual(self.answer(record, code=unknown), "not_found")
        self.assertEqual(self.path(record).read_bytes(), original)

    def test_all_time_and_text_boundaries_accept_expected_limits(self):
        record = self.create(question="宁" * 800, options=tuple("项" * 120 for _ in range(6)), timeout_seconds=900)
        self.assertTrue(self.store.claim_delivery(record.request_id, "主" * 256))
        self.assertEqual(self.answer(record, answer="答" * 2000, owner_key="主" * 256), "accepted")
        self.assertEqual(self.get(record).answer, "答" * 2000)
        self.assertLessEqual(self.path(record).stat().st_size, questions.MAX_RECORD_BYTES)

    def test_capacity_cleans_only_valid_expired_own_records(self):
        unrelated = self.temporary / "keep-me.txt"
        unrelated.write_text("preserve", encoding="utf-8")
        records = [self.create() for _ in range(questions.MAX_RECORDS)]
        with self.assertRaises(QuestionStoreError) as raised:
            self.create()
        self.assertEqual(raised.exception.code, "capacity")
        self.assertEqual(len(self.store.list_pending()), questions.MAX_RECORDS)
        unrecognized = self.root / "my-extension.json"
        unrecognized.write_text("preserve", encoding="utf-8")
        self.now += timedelta(seconds=60)
        newest = self.create()
        self.assertEqual(self.store.list_pending(), (newest,))
        self.assertFalse(any(self.path(record).exists() for record in records))
        self.assertEqual(unrecognized.read_text("utf-8"), "preserve")
        self.assertEqual(unrelated.read_text("utf-8"), "preserve")

    def test_random_code_and_request_collisions_are_retried_without_overwrite(self):
        record = self.create()
        original = self.path(record).read_bytes()
        values = [record.request_id, "1234abcd", "1" * 32, record.code.lower(), "2" * 32, "bbbbbbbb"]
        with patch.object(questions.secrets, "token_hex", side_effect=values):
            created = self.create()
        self.assertEqual(created.request_id, "2" * 32)
        self.assertEqual(created.code, "BBBBBBBB")
        self.assertEqual(self.path(record).read_bytes(), original)

    def test_nonblocking_lock_conflict_fails_closed_without_rewrite(self):
        record = self.create()
        self.store.claim_delivery(record.request_id, "synthetic-owner")
        original = self.path(record).read_bytes()
        with questions._question_lock(self.root.resolve()) as locked:
            self.assertTrue(locked)
            self.assertFalse(self.store.claim_delivery(record.request_id, "synthetic-owner"))
            self.assertFalse(self.store.cancel(record.request_id, **self.binding))
            self.assertEqual(self.answer(record), "busy")
            with self.assertRaises(QuestionStoreError) as raised:
                self.create()
            self.assertEqual(raised.exception.code, "busy")
        self.assertEqual(self.path(record).read_bytes(), original)
        self.assertEqual(self.answer(record), "accepted")

    def test_unknown_lock_file_is_not_modified(self):
        self.root.mkdir()
        path = self.root / questions._LOCK_NAME
        path.write_text("unrelated lock", encoding="utf-8")
        with self.assertRaises(QuestionStoreError) as raised:
            self.create()
        self.assertEqual(raised.exception.code, "busy")
        self.assertEqual(path.read_text("utf-8"), "unrelated lock")

    def test_concurrent_creators_and_answers_do_not_collide_or_replace_first_answer(self):
        def make(_index):
            try:
                return self.create()
            except QuestionStoreError as error:
                self.assertEqual(error.code, "busy")
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            created = [record for record in pool.map(make, range(8)) if record is not None]
        self.assertTrue(created)
        self.assertEqual(len({record.code for record in created}), len(created))
        self.assertEqual(len({record.request_id for record in created}), len(created))
        record = created[0]
        self.assertTrue(self.store.claim_delivery(record.request_id, "synthetic-owner"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda index: self.answer(record, answer=f"answer-{index}",
                message_id=f"message-{index}"), range(8)))
        self.assertEqual(results.count("accepted"), 1)
        self.assertTrue(set(results) <= {"accepted", "busy", "already_answered"})
        self.assertEqual(self.get(record).answer, f"answer-{results.index('accepted')}")

    def test_corrupt_unknown_envelope_and_duplicate_fields_are_never_overwritten(self):
        record = self.create()
        for raw in (b"not-json", b"\xff", b"{}", b'{"format":"foreign","encrypted_state":"text"}',
                    b'{"format":"x","format":"y","encrypted_state":"text"}',
                    b" " * (questions.MAX_RECORD_BYTES + 1)):
            with self.subTest(raw=raw[:40]):
                self.path(record).write_bytes(raw)
                with self.assertRaises(QuestionStoreError) as raised:
                    self.create()
                self.assertEqual(raised.exception.code, "storage_error")
                self.assertFalse(self.store.claim_delivery(record.request_id, "owner"))
                self.assertEqual(self.answer(record), "storage_error")
                self.assertEqual(self.path(record).read_bytes(), raw)

    def test_strict_state_and_filename_validation_rejects_tampering(self):
        record = self.create()
        original = self.path(record).read_bytes()
        changes = ({"prompt": "PRIVATE"}, {"schema_version": True}, {"producer": "foreign"},
                   {"request_id": "0" * 32}, {"code": "lowercase"}, {"source": "other"},
                   {"task_id": "../escape"}, {"created_at": "2026-10-25T00:00:01Z"},
                   {"expires_at": "2026-10-25T00:00:59Z"}, {"status": "executed"},
                   {"status": "answered", "answer": "unclaimed"}, {"owner_key": "owner"},
                   {"delivery_attempted": 1}, {"reply_message_hash": "bad"}, {"options": "not-list"})
        for change in changes:
            with self.subTest(change=change):
                self.path(record).write_bytes(original)
                self.replace_state(record, **change)
                with self.assertRaises(QuestionStoreError):
                    self.store.list_pending()
                self.assertFalse(self.store.claim_delivery(record.request_id, "owner"))
        self.path(record).write_bytes(original)
        self.path(record).rename(self.root / (questions.PREFIX + "f" * 32 + ".json"))
        with self.assertRaises(QuestionStoreError):
            self.store.list_pending()

    def test_plaintext_fallback_and_crypto_faults_raise_only_fixed_error(self):
        for encoder in (lambda value: value, lambda _value: "", lambda _value: None,
                        lambda _value: (_ for _ in ()).throw(RuntimeError("PRIVATE ERROR BODY"))):
            store = self.make_store(protect=encoder)
            with self.subTest(encoder=encoder), self.assertRaises(QuestionStoreError) as raised:
                store.create(**self.binding, project="Example", question="PRIVATE QUESTION")
            self.assertEqual(str(raised.exception), "storage_error")
            self.assertIsNone(raised.exception.__context__)
            self.assertFalse(list(self.root.glob(questions.PREFIX + "*.json")))
        with patch.object(questions, "protect_secret", side_effect=RuntimeError("PRIVATE DPAPI ERROR")):
            store = QuestionStore(self.root, clock=lambda: self.now)
            with self.assertRaises(QuestionStoreError) as raised:
                store.create(**self.binding, project="Example", question="PRIVATE QUESTION")
            self.assertEqual(str(raised.exception), "storage_error")
            self.assertIsNone(raised.exception.__context__)

    def test_atomic_write_failure_preserves_existing_file_and_cleans_only_own_temp(self):
        record = self.create()
        self.store.claim_delivery(record.request_id, "synthetic-owner")
        original = self.path(record).read_bytes()
        unrelated = self.root / ".nene-question-other.tmp"
        unrelated.write_text("user extension", encoding="utf-8")
        with patch.object(questions.os, "replace", side_effect=PermissionError("PRIVATE ERROR")):
            self.assertEqual(self.answer(record), "storage_error")
        self.assertEqual(self.path(record).read_bytes(), original)
        self.assertEqual(unrelated.read_text("utf-8"), "user extension")
        self.assertEqual(list(self.root.glob(".nene-question-*.tmp")), [unrelated])

    def test_root_and_ancestor_links_reparse_points_and_escape_paths_are_rejected(self):
        for root in (Path("relative"), self.temporary / ".." / "outside", Path("C:\\")):
            with self.subTest(root=str(root)), self.assertRaises(QuestionStoreError) as raised:
                QuestionStore(root, protect=protect, unprotect=unprotect)
            self.assertEqual(raised.exception.code, "invalid_root")
        actual_is_link = questions._is_link
        with patch.object(questions, "_is_link", side_effect=lambda path: path == self.temporary or actual_is_link(path)):
            with self.assertRaises(QuestionStoreError):
                self.make_store()
        with patch.object(Path, "is_symlink", return_value=False), \
                patch.object(Path, "is_junction", return_value=False, create=True), \
                patch.object(Path, "lstat", return_value=SimpleNamespace(st_file_attributes=0x400)):
            self.assertTrue(questions._is_link(self.root))

    def test_leaf_links_and_root_changed_after_constructor_do_not_get_written(self):
        record = self.create()
        original = self.path(record).read_bytes()
        actual_is_link = questions._is_link
        with patch.object(questions, "_is_link", side_effect=lambda path: path == self.path(record) or actual_is_link(path)):
            self.assertFalse(self.store.claim_delivery(record.request_id, "synthetic-owner"))
            with self.assertRaises(QuestionStoreError):
                self.get(record)
        with patch.object(questions, "_is_link", side_effect=lambda path: path == self.root or actual_is_link(path)):
            with self.assertRaises(QuestionStoreError):
                self.create()
            self.assertEqual(self.answer(record), "storage_error")
        self.assertEqual(self.path(record).read_bytes(), original)

    def test_scan_is_bounded_without_deleting_other_files(self):
        record = self.create()
        paths = [self.root / f"extension-{index}.txt" for index in range(5)]
        for path in paths:
            path.write_text("keep", encoding="utf-8")
        with patch.object(questions, "MAX_DIRECTORY_ENTRIES", 4):
            with self.assertRaises(QuestionStoreError):
                self.store.list_pending()
            with self.assertRaises(QuestionStoreError):
                self.create()
        self.assertEqual(self.get(record), record)
        self.assertTrue(all(path.read_text("utf-8") == "keep" for path in paths))


if __name__ == "__main__":
    unittest.main()
