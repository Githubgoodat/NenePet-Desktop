"""Independent, bounded question notices projected only from tool metadata.

A question is an observed request, never proof that a task has paused. Keeping
these immutable notices separate from task snapshots prevents a subsequent
running event from erasing a short-lived asynchronous question before polling.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile

from aipet.core.workflow_events import STALE_AFTER, _ID, _inbox_lock, _now, _timestamp


__all__ = ["InputNotice", "publish_input_notice", "load_input_notices"]

MAX_NOTICES = 128
MAX_NOTICE_BYTES = 4096
MAX_DIRECTORY_ENTRIES = 2048
DIRECTORY = "input-notices"
PREFIX = "nene-input-notice-"
PRODUCER = "nenepet-workflow-input-notice"
_FILE = re.compile(r"nene-input-notice-[0-9a-f]{64}\.json\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOOLS = {
    "request_user_input": "input_requested",
    "functions.request_user_input": "input_requested",
    "request_user_input_async": "question_opened",
    "functions.request_user_input_async": "question_opened",
}
_STATUSES = frozenset(_TOOLS.values())
_FIELDS = {"schema_version", "producer", "source", "notice_id", "session_id",
           "turn_id", "tool_use_id", "project_basename", "status", "observed_at"}


@dataclass(frozen=True)
class InputNotice:
    notice_id: str
    task_id: str
    turn_id: str
    project: str
    status: str
    observed_at: str
    source: str = "codex"

    def is_stale(self, now: datetime | None = None) -> bool:
        return _now(now) - _timestamp(self.observed_at) >= STALE_AFTER


def _identity(session: str, turn: str, tool_use: str, status: str) -> str:
    for value in (session, turn, tool_use):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError("Invalid input-notice id")
    if not isinstance(status, str) or status not in _STATUSES:
        raise ValueError("Invalid input-notice status")
    return hashlib.sha256("\0".join(("codex", session, turn, tool_use, status)).encode("utf-8")).hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _has_link(path: Path) -> bool:
    return any(_is_link(part) for part in (path, *path.parents))


def _directory(inbox: Path, *, create: bool) -> Path | None:
    if not isinstance(inbox, Path) or not inbox.is_absolute() or ".." in inbox.parts or _has_link(inbox):
        return None
    if create:
        inbox.mkdir(parents=True, exist_ok=True)
    if not inbox.is_dir() or _has_link(inbox):
        return None
    root = inbox.resolve(strict=True)
    directory = root / DIRECTORY
    if _has_link(directory):
        return None
    if create:
        directory.mkdir(exist_ok=True)
    if not directory.is_dir() or _has_link(directory):
        return None
    resolved = directory.resolve(strict=True)
    return resolved if resolved.parent == root and resolved.name == DIRECTORY else None


def _parse(value: object, current: datetime) -> InputNotice:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("Invalid input-notice fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Invalid input-notice schema")
    if value["producer"] != PRODUCER or value["source"] != "codex":
        raise ValueError("Unsupported input-notice producer")
    identity = _identity(value["session_id"], value["turn_id"], value["tool_use_id"], value["status"])
    if not isinstance(value["notice_id"], str) or not _DIGEST.fullmatch(value["notice_id"]) or value["notice_id"] != identity:
        raise ValueError("Invalid input-notice digest")
    project = value["project_basename"]
    if (not isinstance(project, str) or not 1 <= len(project) <= 96
            or project in {".", ".."} or any(c in "/\\" or not c.isprintable() for c in project)):
        raise ValueError("Invalid input-notice project")
    observed = _timestamp(value["observed_at"])
    if observed > current:
        raise ValueError("Future input notice")
    return InputNotice(
        notice_id=identity, source="codex", task_id=value["session_id"],
        turn_id=value["turn_id"], project=project, status=value["status"],
        observed_at=observed.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate input-notice field")
        value[key] = item
    return value


def _read(path: Path, directory: Path, current: datetime) -> InputNotice | None:
    try:
        # The containing directory and its ancestors were already checked by
        # _directory; verify each leaf and its resolved parent without repeating
        # all ancestor filesystem queries for every retained notice.
        if (_is_link(path) or path.resolve(strict=True).parent != directory or not path.is_file()
                or not 0 < path.stat().st_size < MAX_NOTICE_BYTES):
            return None
        with path.open("rb") as stream:
            raw = stream.read(MAX_NOTICE_BYTES)
        if not 0 < len(raw) < MAX_NOTICE_BYTES:
            return None
        notice = _parse(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object), current)
        return notice if path.name == PREFIX + notice.notice_id + ".json" else None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _scan(directory: Path, current: datetime) -> list[tuple[Path, InputNotice]]:
    records = []
    with os.scandir(directory) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_DIRECTORY_ENTRIES:
                raise ValueError("Input-notice directory scan limit")
            if _FILE.fullmatch(entry.name):
                path = directory / entry.name
                notice = _read(path, directory, current)
                if notice is not None:
                    records.append((path, notice))
    return records


def load_input_notices(inbox: Path, now: datetime | None = None) -> tuple[InputNotice, ...]:
    """Read bounded notices newest first, keeping distinct questions separate.

    Reading creates no directory and does not remove stale or damaged files.
    Consumers deduplicate by notice_id and decide whether fresh notices merit
    a reminder; an old notice is not evidence of a still-unanswered question.
    """
    try:
        current = _now(now)
        directory = _directory(inbox, create=False)
        if directory is None:
            return ()
        records = _scan(directory, current)
        records.sort(key=lambda item: (item[1].observed_at, item[1].notice_id), reverse=True)
        return tuple(notice for _path, notice in records[:MAX_NOTICES])
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return ()


def publish_input_notice(inbox: Path, payload: object, *, now: datetime | None = None) -> bool:
    """Persist exact allowlisted PreToolUse metadata, ignoring all tool content.

    A repeated valid identity is already accepted and returns True without
    refreshing its original time. At capacity, only validated stale notices
    may be evicted. Unknown or damaged files are never replaced or deleted.
    """
    temporary = None
    directory = None
    try:
        current = _now(now)
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
            return False
        tool = payload.get("tool_name")
        if not isinstance(tool, str) or tool not in _TOOLS:
            return False
        status = _TOOLS[tool]
        session, turn, tool_use = (payload.get(key) for key in ("session_id", "turn_id", "tool_use_id"))
        identity = _identity(session, turn, tool_use, status)
        cwd = payload.get("cwd")
        project = PureWindowsPath(cwd).name if isinstance(cwd, str) else ""
        value = {"schema_version": 1, "producer": PRODUCER, "source": "codex",
                 "notice_id": identity, "session_id": session, "turn_id": turn,
                 "tool_use_id": tool_use, "project_basename": project[:96] or "未命名项目",
                 "status": status,
                 "observed_at": current.isoformat(timespec="microseconds").replace("+00:00", "Z")}
        _parse(value, current)
        encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) >= MAX_NOTICE_BYTES:
            return False
        directory = _directory(inbox, create=True)
        if directory is None:
            return False
        with _inbox_lock(directory) as locked:
            if not locked:
                return False
            records = _scan(directory, current)
            destination = directory / (PREFIX + identity + ".json")
            previous = next((notice for path, notice in records if path == destination), None)
            if previous is not None:
                return True
            if destination.exists() or destination.is_symlink():
                return False
            if len(records) > MAX_NOTICES:
                return False  # Do not extend an externally overfilled channel.
            evict = None
            if len(records) >= MAX_NOTICES:
                expired = [(path, notice) for path, notice in records if notice.is_stale(current)]
                if not expired:
                    return False
                evict = min(expired, key=lambda item: (item[1].observed_at, item[1].notice_id))[0]
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".nene-input-notice-", suffix=".tmp",
                                             dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if evict is not None:
                expired_notice = _read(evict, directory, current)
                if expired_notice is None or not expired_notice.is_stale(current):
                    return False
                evict.unlink()
            os.replace(temporary, destination)
            temporary = None
            return True
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return False
    finally:
        if temporary is not None and directory is not None:
            try:
                if not _has_link(temporary) and temporary.resolve().parent == directory:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass
