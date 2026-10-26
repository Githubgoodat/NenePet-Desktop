"""Small, metadata-only heartbeat for the explicitly enabled question channel.

Readers create nothing. Writers only use an already existing dedicated root;
neither operation follows links or reads account/configuration information.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile


STATUS_FILE = "channel-status.json"
MAX_STATUS_BYTES = 2048
STATUS_MAX_AGE = timedelta(seconds=20)
_PRODUCER = "nenepet-workflow-question-channel"
_FIELDS = {"schema_version", "producer", "enabled", "accepting", "observed_at"}


def _now(value=None):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware clock is required")
    return value.astimezone(timezone.utc)


def _linked(info):
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _root(value):
    path = Path(value)
    if (not path.is_absolute() or not path.name or ".." in path.parts
            or str(path).startswith(("\\\\", "//"))
            or any(":" in part for part in path.parts if part != path.anchor)):
        raise ValueError("Invalid channel root")
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Invalid channel root")
    return path, path.stat()


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate status field")
        value[key] = item
    return value


def _read_value(path):
    before = path.lstat()
    if (_linked(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or not 0 < before.st_size <= MAX_STATUS_BYTES):
        raise ValueError("Invalid channel status")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
            raise ValueError("Invalid channel status")
        raw = stream.read(MAX_STATUS_BYTES + 1)
    if len(raw) > MAX_STATUS_BYTES:
        raise ValueError("Invalid channel status")
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["producer"] != _PRODUCER or type(value["enabled"]) is not bool
            or type(value["accepting"]) is not bool
            or not isinstance(value["observed_at"], str) or len(value["observed_at"]) > 40):
        raise ValueError("Invalid channel status")
    observed = _now(datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00")))
    if value["observed_at"] != observed.isoformat(timespec="microseconds").replace("+00:00", "Z"):
        raise ValueError("Noncanonical status timestamp")
    return value, observed


def read_channel_status(inbox: Path, now: datetime | None = None) -> str:
    """Return ready/disabled/paused/offline without creating files or directories."""
    try:
        current = _now(now)
        root, _identity = _root(inbox)
        value, observed = _read_value(root / STATUS_FILE)
        if observed > current or current - observed > STATUS_MAX_AGE:
            return "offline"
        if not value["enabled"]:
            return "disabled"
        return "ready" if value["accepting"] else "paused"
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return "offline"


def write_channel_status(inbox: Path, *, enabled: bool, accepting: bool,
                         now: datetime | None = None) -> None:
    """Atomically refresh our heartbeat in an existing safe dedicated root.

    Failure is quiet and fails closed when readers next check the heartbeat.
    Unknown/corrupt files at our fixed filename are preserved, not overwritten.
    """
    temporary = None
    root = original = None
    try:
        if type(enabled) is not bool or type(accepting) is not bool:
            return
        current = _now(now)
        root, original = _root(inbox)
        destination = root / STATUS_FILE
        if destination.exists() or destination.is_symlink():
            _read_value(destination)
        value = {"schema_version": 1, "producer": _PRODUCER, "enabled": enabled,
                 "accepting": bool(enabled and accepting),
                 "observed_at": current.isoformat(timespec="microseconds").replace("+00:00", "Z")}
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        with tempfile.NamedTemporaryFile(mode="wb", dir=root, prefix=".channel-status-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _same_root, latest = _root(root)
        if (original.st_dev, original.st_ino) != (latest.st_dev, latest.st_ino):
            return
        if destination.exists() or destination.is_symlink():
            _read_value(destination)
        os.replace(temporary, destination)
        temporary = None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return
    finally:
        if temporary is not None and root is not None and original is not None:
            try:
                _same_root, latest = _root(root)
                if (original.st_dev, original.st_ino) == (latest.st_dev, latest.st_ino):
                    temporary.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                pass
