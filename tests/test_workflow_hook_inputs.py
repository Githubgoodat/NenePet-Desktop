"""End-to-end observer projection with synthetic payloads and local files."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from aipet.core.workflow_events import load_snapshots
from aipet.core.workflow_input_notices import load_input_notices


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("input_notice_hook_check", ROOT / "tools/nene_workflow_hook.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class HookInputProjectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="nene-hook-input-"))).resolve()
        self.inbox = self.root / "synthetic-inbox"

    def payload(self, tool="request_user_input_async", event="PreToolUse"):
        return {
            "session_id": "synthetic-task", "turn_id": "synthetic-turn",
            "hook_event_name": event, "tool_name": tool, "tool_use_id": "call-synthetic-1",
            "cwd": "C:\\Synthetic\\Project",
            "tool_input": {"questions": [{"question": "PRIVATE-QUESTION-SENTINEL"}]},
            "transcript_path": "PRIVATE-TRANSCRIPT-SENTINEL",
        }

    def invoke(self, value):
        output = io.StringIO()
        source = io.BytesIO(json.dumps(value).encode("utf-8"))
        with redirect_stdout(output):
            code = hook.main(["--inbox", str(self.inbox)], stdin=source)
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "{}\n")

    def test_question_survives_lifecycle_replacement_and_content_is_excluded(self):
        self.invoke(self.payload())
        self.invoke(self.payload("Bash"))
        self.invoke(self.payload("Bash", "Stop"))
        notices = load_input_notices(self.inbox)
        snapshots = load_snapshots(self.inbox)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].status, "question_opened")
        self.assertEqual(snapshots[0].event, "Stop")
        for path in self.inbox.rglob("*.json"):
            self.assertNotIn("PRIVATE-", path.read_text(encoding="utf-8"))

    def test_sync_and_async_have_distinct_factual_statuses(self):
        for tool, status in (("request_user_input", "input_requested"),
                             ("request_user_input_async", "question_opened")):
            payload = self.payload(tool)
            payload["tool_use_id"] = "call-" + tool
            self.invoke(payload)
        self.assertEqual({row.status for row in load_input_notices(self.inbox)},
                         {"input_requested", "question_opened"})

    def test_unknown_tools_do_not_become_questions_via_their_arguments(self):
        self.invoke(self.payload("Bash"))
        self.assertEqual(load_input_notices(self.inbox), ())

    def test_lifecycle_failure_does_not_disable_question_channel(self):
        with patch.object(hook, "publish_hook_event", side_effect=RuntimeError("private error")):
            self.invoke(self.payload())
        self.assertEqual(len(load_input_notices(self.inbox)), 1)

    def test_question_failure_does_not_disable_lifecycle_or_leak_error(self):
        with patch.object(hook, "publish_input_notice", side_effect=RuntimeError("private error")):
            self.invoke(self.payload())
        self.assertEqual(len(load_snapshots(self.inbox)), 1)
        self.assertEqual(load_input_notices(self.inbox), ())

    def test_oversized_payload_is_ignored_by_both_channels(self):
        payload = self.payload()
        payload["tool_input"] = "X" * hook.MAX_INPUT_BYTES
        self.invoke(payload)
        self.assertFalse(self.inbox.exists())

    def test_retry_of_same_tool_call_does_not_create_duplicate_notice(self):
        self.invoke(self.payload())
        first = load_input_notices(self.inbox)[0]
        self.invoke(self.payload())
        self.assertEqual(load_input_notices(self.inbox), (first,))


if __name__ == "__main__":
    unittest.main()
