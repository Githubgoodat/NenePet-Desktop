"""Explicit metadata-only phase reporting convention for a cooperating Grok Bot.

No Grok installation, token, account, database or native task API is accessed.
accepted means this observer wrote a snapshot, not that another app received it.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aipet.core.workflow_events import publish_task_event


_EVENTS = {
    "running": "GrokRunning", "needs-input": "GrokNeedsInput",
    "phase-ended": "GrokPhaseEnded", "interrupted": "GrokInterrupted",
    "failed": "GrokFailed",
}


class _QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid event arguments")


def main(argv: list[str] | None = None) -> int:
    accepted = False
    try:
        parser = _QuietParser(add_help=False)
        parser.add_argument("--inbox", type=Path, required=True)
        parser.add_argument("--task-id", required=True)
        parser.add_argument("--phase-id", required=True)
        parser.add_argument("--event", choices=tuple(_EVENTS), required=True)
        parser.add_argument("--project", required=True)
        args = parser.parse_args(argv)
        if args.inbox.is_absolute():
            accepted = publish_task_event(args.inbox, task_id=args.task_id, phase_id=args.phase_id,
                                          event=_EVENTS[args.event], project=args.project)
    except Exception:
        pass
    try:
        if sys.stdout is not None:
            sys.stdout.write('{"accepted":true}\n' if accepted else '{"accepted":false}\n')
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
