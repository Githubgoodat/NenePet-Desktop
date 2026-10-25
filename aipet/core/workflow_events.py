"""Small local task-event snapshots, independent of Qt and conversation memory.

Only explicit producer events are facts. A stopped turn is not task success;
absence of updates is stale information, not failure. No transcript is read.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile


MAX_SNAPSHOT_BYTES = 4096
MAX_SNAPSHOTS = 128
MAX_DIRECTORY_ENTRIES = 2048
STALE_AFTER = timedelta(minutes=10)
PREFIX = "nene-workflow-"
PRODUCER = "nenepet-workflow-hook"
EVENT_STATUSES = {
    "UserPromptSubmit": "running", "PreToolUse": "running",
    "PermissionRequest": "needs_attention", "Stop": "turn_ended",
    "Interrupt": "interrupted", "SessionEnd": "session_ended",
}
GROK_EVENT_STATUSES = {
    "GrokRunning": "running", "GrokNeedsInput": "needs_attention",
    "GrokPhaseEnded": "phase_ended", "GrokInterrupted": "interrupted",
    "GrokFailed": "failed",
}
_SOURCE_EVENTS = {"codex": EVENT_STATUSES, "grok_bot": GROK_EVENT_STATUSES}
_TERMINAL = {"turn_ended", "phase_ended", "interrupted", "session_ended", "failed"}
_FIELDS = {"schema_version", "producer", "source", "session_id", "turn_id",
           "event", "observed_at", "project_basename"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FILE = re.compile(r"nene-workflow-[0-9a-f]{64}\.json\Z")


def _now(value: datetime | None = None) -> datetime:
    value = value if value is not None else datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware datetime is required")
    return value.astimezone(timezone.utc)


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid timestamp")
    return _now(datetime.fromisoformat(value.replace("Z", "+00:00")))


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    source: str
    project: str
    event: str
    status: str
    observed_at: str
    turn_id: str

    def is_stale(self, now: datetime | None = None) -> bool:
        return _now(now) - _timestamp(self.observed_at) >= STALE_AFTER


def _filename(session_id: str, turn_id: str, source: str = "codex") -> str:
    # Preserve the original Codex naming while separating other producers.
    identity = session_id + "\0" + turn_id
    if source != "codex":
        identity = source + "\0" + identity
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return PREFIX + digest + ".json"


def _parse(value: object, now: datetime) -> TaskSnapshot:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("Invalid snapshot fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Invalid schema")
    source = value["source"]
    if value["producer"] != PRODUCER or not isinstance(source, str) or source not in _SOURCE_EVENTS:
        raise ValueError("Unsupported source")
    session, turn, event = value["session_id"], value["turn_id"], value["event"]
    if not isinstance(session, str) or not _ID.fullmatch(session):
        raise ValueError("Invalid task id")
    if not isinstance(event, str) or event not in _SOURCE_EVENTS[source]:
        raise ValueError("Unsupported event")
    if not isinstance(turn, str) or (not turn and event != "SessionEnd") or (turn and not _ID.fullmatch(turn)):
        raise ValueError("Invalid turn id")
    project = value["project_basename"]
    if (not isinstance(project, str) or not 1 <= len(project) <= 96
            or project in {".", ".."} or any(c in "/\\" or not c.isprintable() for c in project)):
        raise ValueError("Invalid project label")
    observed = _timestamp(value["observed_at"])
    if observed > now + timedelta(seconds=60):
        raise ValueError("Future snapshot")
    return TaskSnapshot(session, source, project, event, _SOURCE_EVENTS[source][event],
                        observed.isoformat(timespec="microseconds").replace("+00:00", "Z"), turn)


def _read(path: Path, inbox: Path, now: datetime) -> TaskSnapshot | None:
    try:
        if (path.is_symlink() or path.resolve().parent != inbox or not path.is_file()
                or not 0 < path.stat().st_size < MAX_SNAPSHOT_BYTES):
            return None
        with path.open("rb") as stream:
            raw = stream.read(MAX_SNAPSHOT_BYTES)
        if len(raw) >= MAX_SNAPSHOT_BYTES:
            return None
        snapshot = _parse(json.loads(raw), now)
        return snapshot if path.name == _filename(snapshot.task_id, snapshot.turn_id, snapshot.source) else None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _scan(inbox: Path, now: datetime) -> list[tuple[Path, TaskSnapshot]]:
    records = []
    with os.scandir(inbox) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_DIRECTORY_ENTRIES:
                raise ValueError("Inbox scan limit")
            if _FILE.fullmatch(entry.name):
                path = inbox / entry.name
                snapshot = _read(path, inbox, now)
                if snapshot is not None:
                    records.append((path, snapshot))
    return records


def load_snapshots(inbox: Path, now: datetime | None = None) -> tuple[TaskSnapshot, ...]:
    """Read existing own snapshots, newest turn per task; create nothing."""
    try:
        current = _now(now)
        if inbox.is_symlink() or not inbox.is_dir():
            return ()
        records = _scan(inbox.resolve(), current)
        records.sort(key=lambda item: (item[1].observed_at, item[1].event), reverse=True)
        latest = {}
        for _path, snapshot in records:
            latest.setdefault((snapshot.source, snapshot.task_id), snapshot)
        return tuple(list(latest.values())[:MAX_SNAPSHOTS])
    except (OSError, ValueError, TypeError):
        return ()


@contextmanager
def _inbox_lock(inbox: Path):
    """Nonblocking process lock: an observer must never stall its producer."""
    path = inbox / ".nenepet-workflow-v1.lock"
    stream = None
    locked = False
    try:
        if not path.is_symlink() and (not path.exists() or path.is_file()):
            stream = path.open("a+b")
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"NenePet workflow lock\n")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
    except OSError:
        pass
    try:
        yield locked
    finally:
        if stream is not None:
            if locked:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            stream.close()


def _publish_snapshot(inbox: Path, value: dict, current: datetime) -> bool:
    """Write one validated producer snapshot; never execute a producer action.

    Updates are serialized and compared by observation time. Repeated terminal
    observations are accepted without refreshing the original timestamp. A
    terminal event cannot be overwritten by a late nonterminal for that turn.
    At capacity, only validated stale or terminal own snapshots may be evicted.
    """
    temporary = None
    try:
        snapshot = _parse(value, current)
        encoded = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) >= MAX_SNAPSHOT_BYTES or inbox.is_symlink():
            return False
        inbox.mkdir(parents=True, exist_ok=True)
        inbox = inbox.resolve()
        with _inbox_lock(inbox) as locked:
            if not locked:
                return False
            records = _scan(inbox, current)
            destination = inbox / _filename(snapshot.task_id, snapshot.turn_id, snapshot.source)
            old = next((entry for path, entry in records if path == destination), None)
            if destination.exists() and old is None:
                return False  # Never replace an unrecognized or damaged file.
            if (old is not None and old.status in _TERMINAL
                    and old.event == snapshot.event and old.project == snapshot.project):
                # The same event identity is idempotent, not a fresh heartbeat.
                # Stop can be continued by another hook: this snapshot records
                # an observed stop, not proof that the turn cannot run again.
                return True
            if old is not None and (old.observed_at >= snapshot.observed_at
                    or old.status in _TERMINAL and snapshot.status not in _TERMINAL):
                return False
            if old is None and len(records) >= MAX_SNAPSHOTS:
                candidates = [(path, entry) for path, entry in records
                              if entry.status in _TERMINAL or entry.is_stale(current)]
                if not candidates:
                    return False
                oldest_path, _ = min(candidates, key=lambda item: item[1].observed_at)
                oldest_path.unlink()  # Strict own prefix, schema and file-id checked above.
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".nene-workflow-", suffix=".tmp",
                                             dir=inbox, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            temporary = None
            return True
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def publish_hook_event(inbox: Path, payload: object, *, now: datetime | None = None) -> bool:
    """Project only metadata from an official Codex hook, never its contents."""
    try:
        current = _now(now)
        if not isinstance(payload, dict):
            return False
        cwd = payload.get("cwd")
        project = PureWindowsPath(cwd).name if isinstance(cwd, str) else ""
        value = {"schema_version": 1, "producer": PRODUCER, "source": "codex",
                 "session_id": payload.get("session_id"), "turn_id": payload.get("turn_id", ""),
                 "event": payload.get("hook_event_name"),
                 "observed_at": current.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                 "project_basename": project[:96] or "未命名项目"}
        return _publish_snapshot(inbox, value, current)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return False


def publish_task_event(inbox: Path, *, task_id: str, phase_id: str, event: str,
                       project: str, now: datetime | None = None) -> bool:
    """Accept an explicit Grok Bot cooperation report, not native observation.

The caller reports one phase event; an ended phase does not establish task
completion, and absence of reports provides no evidence of current activity.
"""
    try:
        current = _now(now)
        value = {"schema_version": 1, "producer": PRODUCER, "source": "grok_bot",
                 "session_id": task_id, "turn_id": phase_id, "event": event,
                 "observed_at": current.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                 "project_basename": project}
        return _publish_snapshot(inbox, value, current)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return False
