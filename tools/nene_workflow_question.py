"""Explicit, task-bound questions for an opted-in local WeChat reply channel.

This does not discover chats, answer native question cards, approve operations,
or execute phone text. Only an actively waiting caller can continue from a reply.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aipet.core.workflow_question_status import read_channel_status


_SOURCES = ("codex", "grok_bot")
_STATUSES = {"pending", "answered", "cancelled", "expired"}
_STORE_ERRORS = {"invalid_root", "invalid_request", "busy", "capacity", "storage_error"}
MAX_QUESTION_FILE_BYTES = 4096
WAIT_INTERVAL_SECONDS = 0.5


class _ArgumentsError(ValueError):
    pass


class _QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise _ArgumentsError("Invalid arguments")


def _parser():
    parser = _QuietParser(add_help=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_QuietParser)
    for command in ("ask", "get", "cancel"):
        sub = commands.add_parser(command, add_help=False)
        sub.add_argument("--inbox", required=True, type=Path)
        sub.add_argument("--source", required=True, choices=_SOURCES)
        sub.add_argument("--task-id", required=True)
        sub.add_argument("--turn-id", required=True)
        if command == "ask":
            sub.add_argument("--project", required=True)
            question = sub.add_mutually_exclusive_group(required=True)
            question.add_argument("--question")
            question.add_argument("--question-file", type=Path)
            sub.add_argument("--option", action="append", default=[])
            sub.add_argument("--timeout-seconds", type=int, default=600)
        else:
            sub.add_argument("--request-id", required=True)
        if command != "cancel":
            sub.add_argument("--wait-seconds", type=int, default=0)
    return parser


def _question_file(path):
    if (not path.is_absolute() or ".." in path.parts or str(path).startswith(("\\\\", "//"))
            or any(":" in part for part in path.parts if part != path.anchor)):
        raise _ArgumentsError("Invalid question file")
    for component in (path, *path.parents):
        info = component.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise _ArgumentsError("Invalid question file")
    before = path.stat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or not 0 < before.st_size <= MAX_QUESTION_FILE_BYTES):
        raise _ArgumentsError("Invalid question file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
            raise _ArgumentsError("Invalid question file")
        raw = stream.read(MAX_QUESTION_FILE_BYTES + 1)
    if len(raw) > MAX_QUESTION_FILE_BYTES:
        raise _ArgumentsError("Invalid question file")
    return raw.decode("utf-8-sig")


def _make_store(root):
    from aipet.core.workflow_questions import QuestionStore
    return QuestionStore(root)


def _utc_now():
    return datetime.now(timezone.utc)


def _expiry(record):
    value = datetime.fromisoformat(record.expires_at.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Invalid request expiry")
    return value.astimezone(timezone.utc)


def _wait(store, record, binding, seconds, *, clock, monotonic, sleep, channel_status=None):
    if seconds == 0 or record.status != "pending":
        return record, False, None
    remaining = max(0.0, (_expiry(record) - clock()).total_seconds())
    end = monotonic() + min(seconds, remaining)
    while True:
        # A stored answer/cancellation/expiry wins over a concurrent heartbeat
        # change. Never relabel an existing terminal result as merely offline.
        record = store.get(record.request_id, **binding)
        if record is None:
            raise ValueError("Request became unavailable")
        if record.status != "pending":
            return record, False, None
        if channel_status is not None:
            channel = channel_status()
            if channel != "ready":
                channel = channel if channel in {"disabled", "paused", "offline"} else "offline"
                return record, False, channel
        remaining = end - monotonic()
        if remaining <= 0:
            # Only the wait budget ended; the store has not marked expiry.
            return record, True, None
        sleep(min(WAIT_INTERVAL_SECONDS, remaining))


def _result(record, wait_timed_out=False, channel_status=None):
    if record.status not in _STATUSES:
        raise ValueError("Invalid request status")
    result = {"schema_version": 1, "ok": True, "request_id": record.request_id,
              "code": record.code, "source": record.source, "task_id": record.task_id,
              "turn_id": record.turn_id, "status": record.status,
              "created_at": record.created_at, "expires_at": record.expires_at,
              "wait_timed_out": bool(wait_timed_out)}
    if record.status == "answered":
        result["answer"] = record.answer
    if channel_status is not None:
        result["channel_status"] = channel_status
    return result


def _error(code):
    return {"schema_version": 1, "ok": False, "error": code}


def main(argv=None, *, store_factory=None, clock=None, monotonic=None, sleep=None, stdout=None):
    result, exit_code = _error("storage_error"), 1
    record = None
    try:
        args = _parser().parse_args(argv)
        if not args.inbox.is_absolute() or ".." in args.inbox.parts:
            raise _ArgumentsError("Invalid inbox")
        wait_seconds = getattr(args, "wait_seconds", 0)
        if not 0 <= wait_seconds <= 900:
            raise _ArgumentsError("Invalid wait budget")
        clock = clock or _utc_now
        monotonic = monotonic or time.monotonic
        sleep = sleep or time.sleep
        binding = {"source": args.source, "task_id": args.task_id, "turn_id": args.turn_id}
        if args.command == "ask":
            if not 60 <= args.timeout_seconds <= 900:
                raise _ArgumentsError("Invalid question deadline")
            try:
                question = (_question_file(args.question_file)
                            if args.question_file is not None else args.question)
            except (OSError, UnicodeError):
                raise _ArgumentsError("Invalid question file") from None
            if (not isinstance(question, str) or not question.strip() or len(question) > 800
                    or len(args.option) > 6 or any(not item.strip() or len(item) > 120 for item in args.option)):
                raise _ArgumentsError("Invalid question content")
            channel = read_channel_status(args.inbox, now=clock())
            if channel != "ready":
                result = _error("channel_" + channel)
            else:
                store = (store_factory or _make_store)(args.inbox)
                record = store.create(**binding, project=args.project, question=question,
                                      options=tuple(args.option), timeout_seconds=args.timeout_seconds)
                record, timed_out, channel = _wait(
                    store, record, binding, wait_seconds, clock=clock, monotonic=monotonic, sleep=sleep,
                    channel_status=lambda: read_channel_status(args.inbox, now=clock()),
                )
                result, exit_code = _result(record, timed_out, channel), 0
        else:
            store = (store_factory or _make_store)(args.inbox)
            if args.command == "cancel" and not store.cancel(args.request_id, **binding):
                result = _error("not_cancellable")
            else:
                record = store.get(args.request_id, **binding)
                if record is None:
                    result = _error("not_found")
                else:
                    record, timed_out, channel = _wait(
                        store, record, binding, wait_seconds, clock=clock, monotonic=monotonic, sleep=sleep,
                    )
                    result, exit_code = _result(record, timed_out, channel), 0
    except _ArgumentsError:
        result, exit_code = _error("invalid_arguments"), 2
    except KeyboardInterrupt:
        result, exit_code = _error("interrupted"), 130
    except Exception as exc:
        try:
            code = getattr(exc, "code", None)
            code = code if isinstance(code, str) and code in _STORE_ERRORS else "storage_error"
        except Exception:
            code = "storage_error"
        result, exit_code = _error(code), 1
    # Preserve a created request's recovery handle on a later wait interruption
    # or read failure, without claiming its current state or exposing content.
    if not result["ok"] and record is not None:
        try:
            request_id = getattr(record, "request_id", None)
            if (isinstance(request_id, str) and len(request_id) == 32
                    and all(char in "0123456789abcdef" for char in request_id)):
                result["request_id"] = request_id
        except Exception:
            pass
    try:
        target = sys.stdout if stdout is None else stdout
        if target is not None:
            target.write(json.dumps(result, ensure_ascii=True, separators=(",", ":")) + "\n")
    except Exception:
        return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
