"""Codex metadata-only hook observer. Explicit --inbox DIR is required.

All paths are supplied explicitly; no Codex config or transcript is opened.
Malformed input and unavailable inboxes return empty-JSON, nonblocking success.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aipet.core.workflow_events import publish_hook_event
from aipet.core.workflow_input_notices import publish_input_notice

MAX_INPUT_BYTES = 64 * 1024


class _QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid observer arguments")


def main(argv: list[str] | None = None, *, stdin=None) -> int:
    try:
        parser = _QuietParser(add_help=False)
        parser.add_argument("--inbox", type=Path, required=True)
        args = parser.parse_args(argv)
        if not args.inbox.is_absolute():
            return 0
        source = stdin if stdin is not None else sys.stdin
        source = getattr(source, "buffer", source)
        raw = source.read(MAX_INPUT_BYTES + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes) or len(raw) > MAX_INPUT_BYTES:
            return 0
        payload = json.loads(raw)
        # Lifecycle state may be replaced rapidly. Question notices have their
        # own immutable identities so later running/Stop events cannot erase
        # them before the desktop's next poll.
        for projector in (publish_hook_event, publish_input_notice):
            try:
                projector(args.inbox, payload)
            except Exception:
                # One observation channel must not disable the other or
                # interfere with Codex approval and execution.
                pass
    except Exception:
        # An observer must not approve, reject, continue or interrupt Codex.
        pass
    finally:
        # Stop hooks require JSON output. An empty object expresses no decision,
        # adds no context, and contains no event or conversation data.
        try:
            if sys.stdout is not None:
                sys.stdout.write("{}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
