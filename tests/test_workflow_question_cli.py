"""Explicit synthetic questions only; fake codec/clock, no real phone traffic."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import base64
import importlib.util
import io
import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from aipet.core.workflow_questions import QuestionStore, QuestionStoreError
from aipet.core.workflow_question_status import write_channel_status


_CLI_PATH = Path(__file__).resolve().parents[1] / "tools/nene_workflow_question.py"
_SPEC = importlib.util.spec_from_file_location("nene_question_cli_fixture", _CLI_PATH)
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)


def _protect(value):
    return "fixture-only:" + base64.b64encode(value.encode("utf-8")).decode("ascii")


def _unprotect(value):
    if not value.startswith("fixture-only:"):
        raise ValueError("Not synthetic fixture data")
    return base64.b64decode(value[len("fixture-only:"):]).decode("utf-8")


class QuestionCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="question-cli-fixture-"))).resolve()
        self.inbox = self.root / "dedicated-questions"
        self.inbox.mkdir()
        self.now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        self.elapsed, self.sleeps = 0.0, []
        self.on_sleep = None
        self.keep_alive = True
        self.created = []
        self.store = QuestionStore(self.inbox, clock=lambda: self.now,
                                   protect=_protect, unprotect=_unprotect)
        original = self.store.create

        def create(**kwargs):
            record = original(**kwargs)
            self.created.append(record)
            return record

        self.store.create = create
        self.factory = Mock(return_value=self.store)
        self.heartbeat()
        self.binding = {"source": "grok_bot", "task_id": "synthetic-task", "turn_id": "synthetic-phase"}

    def heartbeat(self, enabled=True, accepting=True):
        write_channel_status(self.inbox, enabled=enabled, accepting=accepting, now=self.now)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds
        self.now += timedelta(seconds=seconds)
        if self.keep_alive:
            self.heartbeat()
        if self.on_sleep is not None:
            self.on_sleep()

    def args(self, command="ask", **changes):
        fields = {"inbox": str(self.inbox), "source": self.binding["source"],
                  "task-id": self.binding["task_id"], "turn-id": self.binding["turn_id"]}
        if command == "ask":
            fields |= {"project": "合成项目", "question": "PRIVATE QUESTION FIXTURE", "timeout-seconds": "60"}
        fields.update(changes)
        result = [command]
        for key, value in fields.items():
            if value is not None:
                result.extend(["--" + key, str(value)])
        return result

    def run_cli(self, args, **changes):
        output, errors = io.StringIO(), io.StringIO()
        kwargs = {"store_factory": self.factory, "clock": lambda: self.now,
                  "monotonic": lambda: self.elapsed, "sleep": self.sleep, "stdout": output}
        kwargs.update(changes)
        with redirect_stderr(errors):
            code = cli.main(args, **kwargs)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        return code, json.loads(output.getvalue())

    def answer(self, record, answer="合成回答", message="synthetic-message"):
        self.assertTrue(self.store.claim_delivery(record.request_id, "fixture-owner"))
        self.assertEqual(self.store.submit_reply(code=record.code, answer=answer,
                                                message_id=message, owner_key="fixture-owner"), "accepted")

    def test_default_ask_returns_only_pending_metadata_without_sleep_or_question_echo(self):
        code, value = self.run_cli(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "pending")
        self.assertFalse(value["wait_timed_out"])
        self.assertEqual(value["source"], self.binding["source"])
        self.assertFalse(self.sleeps)
        for field in ("question", "options", "answer", "owner_key", "delivery_attempted"):
            self.assertNotIn(field, value)
        self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertEqual(len(self.created), 1)

    def test_absent_disabled_paused_stale_and_future_channel_never_create_requests(self):
        for state in ("absent", "disabled", "paused", "stale", "future"):
            with self.subTest(state=state):
                self.heartbeat()
                if state == "absent":
                    (self.inbox / "channel-status.json").unlink()
                elif state == "disabled":
                    self.heartbeat(False, False)
                elif state == "paused":
                    self.heartbeat(True, False)
                else:
                    delta = -21 if state == "stale" else 1
                    write_channel_status(self.inbox, enabled=True, accepting=True,
                                         now=self.now + timedelta(seconds=delta))
                code, value = self.run_cli(self.args())
                self.assertEqual(code, 1)
                self.assertEqual(value["error"], "channel_" + (state if state in {"disabled", "paused"} else "offline"))
        self.factory.assert_not_called()
        self.assertFalse(self.created)

    def test_answer_during_bounded_wait_returns_as_data_and_never_runs_shell(self):
        answer = "$(Write-Output PRIVATE); & exit 99\n普通答案"

        def reply():
            if self.elapsed >= 1 and self.created[0].status == "pending":
                record = self.store.get(self.created[0].request_id, **self.binding)
                if record.status == "pending":
                    self.answer(record, answer)

        self.on_sleep = reply
        with patch("subprocess.Popen", side_effect=AssertionError("Never execute phone answers")), \
                patch("os.system", side_effect=AssertionError("Never execute phone answers")):
            code, value = self.run_cli(self.args(**{"wait-seconds": 60}))
        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "answered")
        self.assertEqual(value["answer"], answer)
        self.assertEqual(self.elapsed, 1)
        self.assertTrue(all(0 < delay <= 0.5 for delay in self.sleeps))
        self.assertNotIn("question", value)

    def test_short_wait_budget_remains_pending_while_full_deadline_returns_expired(self):
        code, value = self.run_cli(self.args(**{"wait-seconds": 3}))
        self.assertEqual((code, value["status"], value["wait_timed_out"]), (0, "pending", True))
        self.assertEqual(self.elapsed, 3)
        self.assertNotIn("answer", value)
        code, expired = self.run_cli(self.args(**{"wait-seconds": 900}))
        self.assertEqual((code, expired["status"], expired["wait_timed_out"]), (0, "expired", False))
        self.assertEqual(self.elapsed, 63)
        self.assertNotIn("answer", expired)

    def test_waiting_existing_request_can_observe_cancellation_without_claiming_answer(self):
        record = self.store.create(**self.binding, project="fixture", question="fixture", timeout_seconds=60)
        self.on_sleep = lambda: self.store.cancel(record.request_id, **self.binding)
        code, value = self.run_cli(self.args("get", **{"request-id": record.request_id, "wait-seconds": 4}))
        self.assertEqual((code, value["status"]), (0, "cancelled"))
        self.assertNotIn("answer", value)
        self.assertEqual(self.elapsed, 0.5)

    def test_channel_pause_during_ask_wait_returns_pending_with_route_metadata(self):
        self.on_sleep = lambda: self.heartbeat(True, False)
        code, value = self.run_cli(self.args(**{"wait-seconds": 60}))
        self.assertEqual((code, value["status"], value["channel_status"]), (0, "pending", "paused"))
        self.assertFalse(value["wait_timed_out"])
        self.assertEqual(value["request_id"], self.created[0].request_id)
        self.assertEqual(self.elapsed, 0.5)
        self.assertNotIn("answer", value)

    def test_crashed_channel_stops_ask_wait_after_heartbeat_expiry_without_fake_expired(self):
        self.keep_alive = False
        code, value = self.run_cli(self.args(**{"wait-seconds": 60}))
        self.assertEqual((code, value["status"], value["channel_status"]), (0, "pending", "offline"))
        self.assertLessEqual(self.elapsed, 20.5)
        self.assertNotIn("answer", value)

    def test_stored_answer_or_cancel_wins_over_simultaneous_paused_heartbeat(self):
        for outcome in ("answered", "cancelled"):
            with self.subTest(outcome=outcome):
                self.heartbeat()

                def finish():
                    record = self.created[-1]
                    if outcome == "answered":
                        self.answer(record)
                    else:
                        self.store.cancel(record.request_id, **self.binding)
                    self.heartbeat(True, False)

                self.on_sleep = finish
                code, value = self.run_cli(self.args(**{"wait-seconds": 60}))
                self.assertEqual((code, value["status"]), (0, outcome))
                self.assertNotIn("channel_status", value)

    def test_get_and_cancel_existing_request_do_not_require_channel_online(self):
        _, created = self.run_cli(self.args())
        self.heartbeat(False, False)
        code, pending = self.run_cli(self.args("get", **{"request-id": created["request_id"]}))
        self.assertEqual((code, pending["status"]), (0, "pending"))
        code, cancelled = self.run_cli(self.args("cancel", **{"request-id": created["request_id"]}))
        self.assertEqual((code, cancelled["status"]), (0, "cancelled"))
        code, repeated = self.run_cli(self.args("cancel", **{"request-id": created["request_id"]}))
        self.assertEqual((code, repeated["status"]), (0, "cancelled"))

    def test_strong_binding_rejects_get_and_cancel_for_other_source_task_or_turn(self):
        _, created = self.run_cli(self.args())
        for change in ({"source": "codex"}, {"task-id": "another-task"}, {"turn-id": "another-phase"}):
            for command in ("get", "cancel"):
                with self.subTest(change=change, command=command):
                    code, value = self.run_cli(self.args(command, **{"request-id": created["request_id"], **change}))
                    self.assertEqual(code, 1)
                    self.assertFalse(value["ok"])
                    self.assertNotIn("answer", value)
                    self.assertNotIn("PRIVATE", json.dumps(value))
        self.assertEqual(self.store.get(created["request_id"], **self.binding).status, "pending")

    def test_answer_is_visible_only_for_answered_unexpired_bound_record(self):
        _, created = self.run_cli(self.args())
        self.answer(self.created[0])
        args = self.args("get", **{"request-id": created["request_id"]})
        code, first = self.run_cli(args)
        _, repeated = self.run_cli(args)
        self.assertEqual((code, first["status"], first["answer"]), (0, "answered", "合成回答"))
        self.assertEqual(first, repeated)
        self.now += timedelta(seconds=60)
        _, expired = self.run_cli(args)
        self.assertEqual(expired["status"], "expired")
        self.assertNotIn("answer", expired)

    def test_explicit_new_utf8_question_file_and_options_are_passed_without_echo(self):
        path = self.root / "new-question.txt"
        path.write_text("新建的合成问题？", encoding="utf-8-sig")
        args = self.args(question=None, **{"question-file": path})
        args += ["--option", "第一项", "--option", "第二项"]
        code, value = self.run_cli(args)
        self.assertEqual(code, 0)
        self.assertEqual(self.created[0].question, "新建的合成问题？")
        self.assertEqual(self.created[0].options, ("第一项", "第二项"))
        self.assertNotIn("新建", json.dumps(value, ensure_ascii=False))

    def test_invalid_arguments_and_question_files_never_echo_diagnostic_contents(self):
        invalid = [[], self.args(**{"inbox": "relative"}), self.args(**{"wait-seconds": -1}),
                   self.args(**{"wait-seconds": 901}), self.args(**{"timeout-seconds": 59}),
                   self.args(**{"timeout-seconds": 901}), self.args(question=""),
                   self.args(question="x" * 801), self.args() + ["--unknown", "PRIVATE BAD INPUT"],
                   self.args() + ["--option", "x" * 121], self.args() + ["--option", ""]]
        for args in invalid:
            with self.subTest(args=args[:1]):
                code, value = self.run_cli(args)
                self.assertEqual((code, value["error"]), (2, "invalid_arguments"))
                self.assertNotIn("PRIVATE", json.dumps(value))
        for raw in (b"\xff", b"x" * 4097):
            path = self.root / "invalid-new-question.txt"
            path.write_bytes(raw)
            code, value = self.run_cli(self.args(question=None, **{"question-file": path}))
            self.assertEqual((code, value["error"]), (2, "invalid_arguments"))
        self.factory.assert_not_called()

    def test_storage_errors_and_interruptions_are_safe_json_without_tracebacks(self):
        for exc, expected in ((QuestionStoreError("busy"), "busy"),
                              (RuntimeError("PRIVATE TOKEN\nPRIVATE QUESTION"), "storage_error")):
            code, value = self.run_cli(self.args(), store_factory=Mock(side_effect=exc))
            self.assertEqual((code, value["error"]), (1, expected))
            self.assertNotIn("PRIVATE", json.dumps(value))
        code, value = self.run_cli(self.args(**{"wait-seconds": 60}),
                                   sleep=Mock(side_effect=KeyboardInterrupt))
        self.assertEqual((code, value["error"]), (130, "interrupted"))
        self.assertEqual(value["request_id"], self.created[-1].request_id)
        self.assertNotIn("answer", value)

    def test_script_entrypoint_invalid_arguments_returns_one_json_document(self):
        output, errors = io.StringIO(), io.StringIO()
        with (patch.object(sys, "argv", [str(_CLI_PATH), "ask", "--inbox", "relative"]),
              redirect_stdout(output), redirect_stderr(errors),
              self.assertRaises(SystemExit) as raised):
            runpy.run_path(str(_CLI_PATH), run_name="__main__")
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(output.getvalue())["error"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
