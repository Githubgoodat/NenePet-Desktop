"""Prepare reviewable Codex lifecycle hooks; never install or trust them."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import sys


EVENTS = ("UserPromptSubmit", "PreToolUse", "PermissionRequest", "Stop", "Interrupt", "SessionEnd")


def _windows_command(arguments: list[str]) -> str:
    # Codex uses the selected shell. This Windows generator targets PowerShell:
    # a quoted executable needs '&', and single-quoted arguments stay literal.
    # Keep rejecting previously unsupported characters; other Windows shells
    # require their own reviewed command definition.
    if any(any(c in value for c in '\"%!\r\n\x00') for value in arguments):
        raise ValueError("Windows hook paths contain unsupported shell characters")
    return "& " + " ".join("'" + value.replace("'", "''") + "'" for value in arguments)


def prepare_hooks(python: Path, script: Path, inbox: Path) -> dict:
    if not all(path.is_absolute() for path in (python, script, inbox)):
        raise ValueError("Use absolute Python, script and inbox paths")
    if not python.is_file() or not script.is_file() or script.suffix.lower() != ".py":
        raise ValueError("Python executable and hook script must exist")
    # The explicit inbox is data written by this observer, not an install path.
    if inbox.exists() and (not inbox.is_dir() or inbox.is_symlink()):
        raise ValueError("Inbox must be a directory without a symlink")
    arguments = [str(python), str(script), "--inbox", str(inbox)]
    command = _windows_command(arguments) if sys.platform == "win32" else shlex.join(arguments)
    return {
        "description": "NenePet local task status observer. Review and trust in Codex /hooks; no approval decisions or chat content.",
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
            for event in EVENTS
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, required=True, help="Absolute inbox shown by the pet's AI task panel")
    parser.add_argument("--output", type=Path, required=True, help="A new .json file in an existing directory")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    try:
        script = Path(__file__).resolve().with_name("nene_workflow_hook.py")
        hooks = prepare_hooks(args.python, script, args.inbox)
        target = args.output.expanduser().absolute()
        if target.suffix.lower() != ".json" or target.is_symlink() or not target.parent.is_dir():
            raise ValueError("Output must be a new .json file in an existing directory")
        # Exclusive create leaves an existing configuration or report intact.
        with target.open("x", encoding="utf-8") as stream:
            json.dump(hooks, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except (OSError, ValueError):
        print("未生成接入配置：请使用有效的绝对路径及已有目录中的新 .json 文件；不会覆盖现有配置。", file=sys.stderr)
        return 2
    print("已生成待审阅配置，尚未安装或信任。请按 docs/WORKFLOW.md 接入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
