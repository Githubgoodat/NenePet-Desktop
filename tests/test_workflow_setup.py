"""Generated hook commands must be observers, not approval or inference tools."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nene_workflow_setup", ROOT / "tools/prepare_codex_workflow.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class WorkflowSetupTests(unittest.TestCase):
    def test_windows_paths_with_spaces_are_quoted_and_control_characters_rejected(self):
        self.assertEqual('"C:/Program Files/Python/python.exe" "script.py"',
                         setup._windows_command(["C:/Program Files/Python/python.exe", "script.py"]))
        for bad in ('percent%PATH%', 'quoted"path', 'line\npath', 'bang!path', 'nul\0path'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                setup._windows_command([bad])

    def test_generated_observer_has_only_requested_lifecycle_events_and_no_permission_decision(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            python, script = root / "python.exe", root / "observer.py"
            python.write_bytes(b"synthetic fixture")
            script.write_text("# synthetic fixture", encoding="utf-8")
            inbox = root / "inbox"
            with patch.object(setup.sys, "platform", "win32"):
                hooks = setup.prepare_hooks(python, script, inbox)
            self.assertEqual(set(setup.EVENTS), set(hooks["hooks"]))
            for groups in hooks["hooks"].values():
                handler = groups[0]["hooks"][0]
                self.assertEqual({"type", "command", "timeout"}, set(handler))
                self.assertIn('"--inbox"', handler["command"])
                self.assertEqual(3, handler["timeout"])
            self.assertFalse(inbox.exists())
            self.assertNotIn("permissionDecision", json.dumps(hooks))

    def test_relative_paths_and_missing_script_are_rejected(self):
        with self.assertRaises(ValueError):
            setup.prepare_hooks(Path("python"), Path("script.py"), Path("inbox"))

    def test_existing_configuration_cannot_be_overwritten(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / "hooks.json"
            target.write_text('{"existing":true}', encoding="utf-8")
            with patch.object(setup, "prepare_hooks", return_value={"hooks": {}}):
                code = setup.main(["--inbox", str(root / "inbox"), "--output", str(target)])
            self.assertEqual(2, code)
            self.assertEqual('{"existing":true}', target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
