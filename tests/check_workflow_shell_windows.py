"""Explicit Windows PowerShell check with a synthetic inbox in owned scratch.

This runs subprocesses, so it is deliberately separate from test_*.py discovery
and the public offline runner. It never opens Codex configuration or a real
NenePet inbox. --scratch-root must name an existing absolute directory.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PREFIX = "nenepet-workflow-shell-"
SENTINEL = "PRIVATE-SYNTHETIC-CONTENT-MUST-NOT-BE-SAVED"


class CheckFailure(RuntimeError):
    """A fixed diagnostic safe to print without subprocess contents."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _owned_scratch(root: Path, scratch: Path) -> Path:
    """Resolve and verify the exact random child before use or cleanup."""
    _require(not scratch.is_symlink(), "Scratch directory became a symlink")
    resolved = scratch.resolve(strict=True)
    _require(
        resolved.is_dir() and resolved.parent == root
        and resolved != root and resolved.name.startswith(PREFIX),
        "Scratch directory is outside its expected parent",
    )
    return resolved


def _run_powershell(shell: str, command: str, *, payload: dict | None = None):
    environment = os.environ.copy()
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
    return subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        input=json.dumps(payload, ensure_ascii=True) if payload is not None else "",
        text=True, encoding="utf-8", capture_output=True, timeout=15,
        env=environment,
    )


def _check(root: Path, shell: str) -> None:
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(
        "nene_workflow_shell_checked_setup", ROOT / "tools" / "prepare_codex_workflow.py",
    )
    _require(spec is not None and spec.loader is not None, "Cannot load hook generator")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    sys.path.insert(0, str(ROOT))
    from aipet.core.workflow_events import load_snapshots

    scratch = Path(tempfile.mkdtemp(prefix=PREFIX, dir=root))
    try:
        scratch = _owned_scratch(root, scratch)
        inbox = scratch / "synthetic inbox 宁宁 O'Brien $literal; &"
        project = scratch / "Synthetic Project 宁宁"
        hooks = setup.prepare_hooks(
            Path(sys.executable).resolve(strict=True),
            ROOT / "tools" / "nene_workflow_hook.py", inbox,
        )
        command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        result = _run_powershell(shell, command, payload={
            "hook_event_name": "UserPromptSubmit",
            "session_id": "native-shell-synthetic",
            "turn_id": "synthetic-turn",
            "cwd": str(project),
            "prompt": SENTINEL,
            "transcript_path": SENTINEL,
            "tool_input": {"question": SENTINEL},
        })
        _require(result.returncode == 0, "Native observer command did not exit zero")
        _require(json.loads(result.stdout) == {}, "Observer did not output empty JSON")
        rows = load_snapshots(inbox)
        _require(
            len(rows) == 1 and rows[0].source == "codex"
            and rows[0].event == "UserPromptSubmit"
            and rows[0].task_id == "native-shell-synthetic"
            and rows[0].project == project.name,
            "Synthetic inbox did not contain the expected metadata",
        )
        snapshots = list(inbox.glob("nene-workflow-*.json"))
        _require(len(snapshots) == 1, "Expected exactly one synthetic snapshot")
        saved = snapshots[0].read_text(encoding="utf-8")
        _require(SENTINEL not in saved, "Sensitive placeholder content entered the snapshot")
        _require(
            not {"prompt", "tool_input", "transcript_path"}.intersection(json.loads(saved)),
            "Conversation fields entered the snapshot",
        )

        literal_script = scratch / "O'Brien 宁宁 $literal; & fixture.py"
        literal_script.write_text(
            "import json, sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=True))\n",
            encoding="utf-8",
        )
        values = ["spaces stay together", "宁宁 Unicode", "O'Brien", "$profile; & literal"]
        literal_command = setup._windows_command(
            [sys.executable, str(literal_script), *values],
        )
        literal = _run_powershell(shell, literal_command)
        _require(literal.returncode == 0, "Literal argument command did not exit zero")
        _require(json.loads(literal.stdout) == values, "PowerShell changed literal arguments")
    finally:
        # Never recursively remove a computed path before checking its resolved
        # absolute location. Only this invocation's random child is owned here.
        shutil.rmtree(_owned_scratch(root, scratch))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        _require(sys.platform == "win32", "This native check requires Windows")
        _require(args.scratch_root.is_absolute(), "Scratch root must be absolute")
        root = args.scratch_root.resolve(strict=True)
        _require(root.is_dir(), "Scratch root must be an existing directory")
        shell = shutil.which("powershell.exe")
        _require(shell is not None, "Windows PowerShell was not found")
        _check(root, shell)
    except CheckFailure as error:
        print(json.dumps({"status": "failed", "reason": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Raw output may contain local paths; report only the failure category.
        print(json.dumps({"status": "failed", "reason": type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({
        "status": "passed", "native_powershell_hook": "passed",
        "literal_arguments": "passed", "synthetic_content_excluded": True,
        "real_profile_written": False, "network_used": False,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
