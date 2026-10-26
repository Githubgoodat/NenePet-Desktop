"""Short-lived, encrypted cooperation questions, separate from pet memory.

An answer is ordinary user data. This store never approves a permission,
executes a command, or claims that a collected answer has been acted upon.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from aipet.core.credentials import protect_secret, unprotect_secret
from aipet.core.workflow_events import _ID, _now, _timestamp


__all__ = ["QuestionRecord", "QuestionStore", "QuestionStoreError"]

MAX_RECORDS = 32
MAX_DIRECTORY_ENTRIES = 256
MAX_RECORD_BYTES = 65536
MAX_STATE_BYTES = 24576
PREFIX = "nene-workflow-question-"
FORMAT = "nenepet-workflow-question-v1"
_FILE = re.compile(r"nene-workflow-question-([0-9a-f]{32})\.json\Z")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
_CODE = re.compile(r"[0-9A-F]{8}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LOCK_NAME = ".nene-workflow-questions-v1.lock"
_LOCK_CONTENT = b"NenePet cooperation questions v1\n"
_FIELDS = {"schema_version", "producer", "request_id", "code", "source", "task_id",
           "turn_id", "project", "question", "options", "created_at", "expires_at",
           "status", "answer", "owner_key", "delivery_attempted", "reply_message_hash"}
_ERRORS = {"invalid_root", "invalid_request", "busy", "capacity", "storage_error"}


class QuestionStoreError(Exception):
    """Fixed safe error codes; no paths, question text, or wrapped exceptions."""

    def __init__(self, code="storage_error"):
        self.code = code if isinstance(code, str) and code in _ERRORS else "storage_error"
        super().__init__(self.code)


class _QuestionExpired(Exception):
    pass


@dataclass(frozen=True, repr=False)
class QuestionRecord:
    request_id: str
    code: str
    source: str
    task_id: str
    turn_id: str
    project: str
    question: str
    options: tuple[str, ...]
    created_at: str
    expires_at: str
    status: str
    answer: str
    owner_key: str
    delivery_attempted: bool


def _utc_now():
    return datetime.now(timezone.utc)


def _is_link(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _root_path(path):
    if (not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts
            or not path.name or path.drive.startswith("\\\\")
            or any(":" in part for part in path.parts[1:])
            or any(_is_link(part) for part in (path, *path.parents))):
        raise QuestionStoreError("invalid_root")
    return path


def _text(value, limit, *, allow_empty=False, multiline=False):
    if not isinstance(value, str):
        raise ValueError("Invalid field")
    value = value.strip()
    if (len(value) > limit or not value and not allow_empty
            or any(not char.isprintable() and not (multiline and char in "\r\n\t") for char in value)):
        raise ValueError("Invalid field")
    value.encode("utf-8")
    return value


def _binding(source, task_id, turn_id):
    if (not isinstance(source, str) or source not in {"codex", "grok_bot"}
            or any(not isinstance(value, str) or not _ID.fullmatch(value) for value in (task_id, turn_id))):
        raise ValueError("Invalid binding")
    return source, task_id, turn_id


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate field")
        value[key] = item
    return value


def _parse(value, now):
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["producer"] != FORMAT
            or not isinstance(value["request_id"], str) or not _REQUEST_ID.fullmatch(value["request_id"])
            or not isinstance(value["code"], str) or not _CODE.fullmatch(value["code"])):
        raise ValueError("Invalid question record")
    _binding(value["source"], value["task_id"], value["turn_id"])
    project = _text(value["project"], 96)
    if project in {".", ".."} or any(char in "/\\:" for char in project):
        raise ValueError("Invalid project")
    question = _text(value["question"], 800, multiline=True)
    options = value["options"]
    if not isinstance(options, list) or len(options) > 6:
        raise ValueError("Invalid options")
    options = tuple(_text(option, 120) for option in options)
    created, expires = _timestamp(value["created_at"]), _timestamp(value["expires_at"])
    duration = (expires - created).total_seconds()
    if created > now or not duration.is_integer() or not 60 <= duration <= 900:
        raise ValueError("Invalid question lifetime")
    status = value["status"]
    if status not in {"pending", "answered", "cancelled"} or type(value["delivery_attempted"]) is not bool:
        raise ValueError("Invalid question state")
    answer = _text(value["answer"], 2000, allow_empty=True, multiline=True)
    owner = _text(value["owner_key"], 256, allow_empty=True)
    digest = value["reply_message_hash"]
    if (not isinstance(digest, str) or digest and not _HASH.fullmatch(digest)
            or bool(owner) != value["delivery_attempted"]
            or status == "answered" and not (answer and owner and digest)
            or status != "answered" and (answer or digest)):
        raise ValueError("Invalid answer state")
    # Canonical stored strings cannot silently change while being decrypted.
    if (project != value["project"] or question != value["question"]
            or list(options) != value["options"] or answer != value["answer"] or owner != value["owner_key"]):
        raise ValueError("Noncanonical question fields")
    return QuestionRecord(
        request_id=value["request_id"], code=value["code"], source=value["source"],
        task_id=value["task_id"], turn_id=value["turn_id"], project=project,
        question=question, options=options, created_at=value["created_at"], expires_at=value["expires_at"],
        status="expired" if now >= expires else status,
        answer="" if now >= expires else answer, owner_key=owner,
        delivery_attempted=value["delivery_attempted"],
    )


@contextmanager
def _question_lock(directory):
    """Own, nonblocking process lock; never append to an unrecognized file."""
    path = directory / _LOCK_NAME
    stream, locked = None, False
    try:
        if _is_link(path):
            yield False
            return
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            descriptor = None
        if descriptor is not None:
            with os.fdopen(descriptor, "wb") as created:
                created.write(_LOCK_CONTENT)
                created.flush()
        if (_is_link(path) or not path.is_file() or path.resolve(strict=True).parent != directory
                or path.stat().st_size != len(_LOCK_CONTENT)):
            yield False
            return
        stream = path.open("r+b")
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            pass
        # On Windows, reading a byte locked by another handle also fails.
        # Check our marker only after winning the nonblocking byte lock.
        if locked and stream.read(len(_LOCK_CONTENT) + 1) != _LOCK_CONTENT:
            yield False
            return
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


class QuestionStore:
    def __init__(self, root: Path, *, clock=_utc_now, protect=None, unprotect=None):
        failed = False
        try:
            self.root = _root_path(root)
            self._canonical_root = self.root.resolve()
            if not callable(clock) or (protect is None) != (unprotect is None):
                raise ValueError("Invalid dependencies")
            self._clock = clock
            self._protect = protect if protect is not None else protect_secret
            self._unprotect = unprotect if unprotect is not None else unprotect_secret
            if not callable(self._protect) or not callable(self._unprotect):
                raise ValueError("Invalid dependencies")
        except Exception:
            failed = True
        if failed:
            raise QuestionStoreError("invalid_root")

    def _directory(self, *, create=False):
        path = _root_path(self.root)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            return None
        if (not path.is_dir() or _root_path(path).resolve(strict=True) != self._canonical_root):
            raise QuestionStoreError("storage_error")
        return self._canonical_root

    def _read(self, path, directory, now):
        if (_is_link(path) or path.resolve(strict=True).parent != directory or not path.is_file()
                or not 0 < path.stat().st_size <= MAX_RECORD_BYTES):
            raise ValueError("Invalid record file")
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("Oversized record")
        envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(envelope, dict) or set(envelope) != {"format", "encrypted_state"}
                or envelope["format"] != FORMAT
                or not isinstance(envelope["encrypted_state"], str) or not envelope["encrypted_state"]):
            raise ValueError("Invalid encrypted envelope")
        clear = self._unprotect(envelope["encrypted_state"])
        if not isinstance(clear, str) or not 0 < len(clear.encode("utf-8")) <= MAX_STATE_BYTES:
            raise ValueError("Invalid decrypted record")
        value = json.loads(clear, object_pairs_hook=_unique_object)
        record = _parse(value, now)
        if path.name != PREFIX + record.request_id + ".json":
            raise ValueError("Mismatched record filename")
        return value, record

    def _scan(self, directory, now):
        records, codes = [], set()
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_DIRECTORY_ENTRIES:
                    raise ValueError("Question scan bound")
                if _FILE.fullmatch(entry.name):
                    path = directory / entry.name
                    value, record = self._read(path, directory, now)
                    if record.code in codes:
                        raise ValueError("Duplicate question code")
                    codes.add(record.code)
                    records.append((path, value, record))
                    if len(records) > MAX_RECORDS:
                        raise ValueError("Question capacity exceeded")
        return records

    def _write(self, directory, value, now, *, previous=None):
        record = _parse(value, now)
        path = directory / (PREFIX + record.request_id + ".json")
        clear = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(clear.encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("Oversized question state")
        encrypted = self._protect(clear)
        if not isinstance(encrypted, str) or not encrypted or encrypted == clear:
            raise ValueError("Encryption required")
        raw = json.dumps({"format": FORMAT, "encrypted_state": encrypted},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("Oversized encrypted record")
        temporary = None
        try:
            if self._directory() != directory or _is_link(path):
                raise ValueError("Unsafe question target")
            if path.exists():
                if previous is None or self._read(path, directory, now)[0] != previous:
                    raise ValueError("Question changed outside lock")
            elif previous is not None:
                raise ValueError("Question disappeared")
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".nene-question-", suffix=".tmp",
                                             dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if self._directory() != directory or _is_link(path):
                raise ValueError("Unsafe question target")
            if path.exists():
                if previous is None or self._read(path, directory, now)[0] != previous:
                    raise ValueError("Question changed outside lock")
            elif previous is not None:
                raise ValueError("Question disappeared")
            if _parse(value, _now(self._clock())).status == "expired":
                raise _QuestionExpired()
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    if self._directory() == directory and not _is_link(temporary) and temporary.resolve().parent == directory:
                        temporary.unlink(missing_ok=True)
                except Exception:
                    pass
        return record

    def create(self, *, source, task_id, turn_id, project, question, options=(), timeout_seconds=600):
        failure = "invalid_request"
        try:
            _binding(source, task_id, turn_id)
            project = _text(project, 96)
            question = _text(question, 800, multiline=True)
            if not isinstance(options, (tuple, list)) or len(options) > 6:
                raise ValueError("Invalid options")
            options = [_text(option, 120) for option in options]
            if type(timeout_seconds) is not int or not 60 <= timeout_seconds <= 900:
                raise ValueError("Invalid lifetime")
            now = _now(self._clock())
            value = {"schema_version": 1, "producer": FORMAT, "request_id": "0" * 32,
                     "code": "0" * 8, "source": source, "task_id": task_id, "turn_id": turn_id,
                     "project": project, "question": question, "options": options,
                     "created_at": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                     "expires_at": (now + timedelta(seconds=timeout_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                     "status": "pending", "answer": "", "owner_key": "",
                     "delivery_attempted": False, "reply_message_hash": ""}
            _parse(value, now)
            failure = "storage_error"
            directory = self._directory(create=True)
            with _question_lock(directory) as locked:
                if not locked:
                    raise QuestionStoreError("busy")
                records = self._scan(directory, now)
                for path, _old, record in records:
                    if record.status == "expired":
                        if self._read(path, directory, now)[1].status != "expired":
                            raise ValueError("Changed expired question")
                        path.unlink()
                live = [record for _path, _old, record in records if record.status != "expired"]
                if len(live) >= MAX_RECORDS:
                    raise QuestionStoreError("capacity")
                codes = {record.code for record in live}
                for _attempt in range(32):
                    value["request_id"], value["code"] = secrets.token_hex(16), secrets.token_hex(4).upper()
                    destination = directory / (PREFIX + value["request_id"] + ".json")
                    if value["code"] not in codes and not destination.exists() and not _is_link(destination):
                        return self._write(directory, value, now)
                raise QuestionStoreError("capacity")
        except QuestionStoreError as error:
            failure = error.code
        except Exception:
            pass
        raise QuestionStoreError(failure)

    def list_pending(self):
        try:
            now = _now(self._clock())
            directory = self._directory()
            if directory is None:
                return ()
            scanned = self._scan(directory, now)
            current = _now(self._clock())
            records = [_parse(value, current) for _path, value, _record in scanned]
            records = [record for record in records if record.status == "pending"]
            return tuple(sorted(records, key=lambda record: (record.created_at, record.request_id)))
        except Exception:
            pass
        raise QuestionStoreError("storage_error")

    def get(self, request_id, *, source, task_id, turn_id):
        try:
            identity = _binding(source, task_id, turn_id)
            if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
                return None
        except ValueError:
            return None
        try:
            now = _now(self._clock())
            directory = self._directory()
            if directory is None:
                return None
            path = directory / (PREFIX + request_id + ".json")
            if not path.exists() and not _is_link(path):
                return None
            value, _record = self._read(path, directory, now)
            record = _parse(value, _now(self._clock()))
            return record if (record.source, record.task_id, record.turn_id) == identity else None
        except Exception:
            pass
        raise QuestionStoreError("storage_error")

    def claim_delivery(self, request_id, owner_key):
        try:
            if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
                return False
            owner = _text(owner_key, 256)
            now, directory = _now(self._clock()), self._directory()
            if directory is None:
                return False
            with _question_lock(directory) as locked:
                if not locked:
                    return False
                path = directory / (PREFIX + request_id + ".json")
                if not path.exists():
                    return False
                old, record = self._read(path, directory, now)
                if record.status != "pending" or record.delivery_attempted:
                    return False
                self._write(directory, old | {"owner_key": owner, "delivery_attempted": True}, now, previous=old)
                return True
        except Exception:
            return False

    def submit_reply(self, *, code, answer, message_id, owner_key):
        try:
            if not isinstance(code, str) or not _CODE.fullmatch(code.strip().upper()):
                return "invalid_reply"
            code, answer = code.strip().upper(), _text(answer, 2000, multiline=True)
            owner, message = _text(owner_key, 256), _text(message_id, 256)
            digest = hashlib.sha256((owner + "\0" + message).encode("utf-8")).hexdigest()
        except (ValueError, TypeError, UnicodeError):
            return "invalid_reply"
        try:
            now, directory = _now(self._clock()), self._directory()
            if directory is None:
                return "not_found"
            with _question_lock(directory) as locked:
                if not locked:
                    return "busy"
                records = self._scan(directory, now)
                matched = next((row for row in records if row[2].code == code), None)
                if matched is None:
                    return "not_found"
                _path, old, record = matched
                if record.status in {"expired", "cancelled"}:
                    return record.status
                if not record.delivery_attempted:
                    return "not_delivered"
                if owner != record.owner_key:
                    return "owner_mismatch"
                if any(value["reply_message_hash"] == digest for _path, value, _record in records):
                    return "duplicate_message"
                if record.status == "answered":
                    return "already_answered"
                index = (ord(answer.upper()) - ord("A") if len(answer) == 1 and "A" <= answer.upper() <= "F"
                         else int(answer) - 1 if len(answer) == 1 and "1" <= answer <= "6" else -1)
                if 0 <= index < len(record.options):
                    answer = record.options[index]
                self._write(directory, old | {"answer": answer, "status": "answered",
                                             "reply_message_hash": digest}, now, previous=old)
                return "expired" if _now(self._clock()) >= _timestamp(record.expires_at) else "accepted"
        except _QuestionExpired:
            return "expired"
        except Exception:
            return "storage_error"

    def cancel(self, request_id, *, source, task_id, turn_id):
        try:
            identity = _binding(source, task_id, turn_id)
            if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
                return False
            now, directory = _now(self._clock()), self._directory()
            if directory is None:
                return False
            with _question_lock(directory) as locked:
                if not locked:
                    return False
                path = directory / (PREFIX + request_id + ".json")
                if not path.exists():
                    return False
                old, record = self._read(path, directory, now)
                if (record.source, record.task_id, record.turn_id) != identity:
                    return False
                if record.status == "cancelled":
                    return True
                if record.status != "pending":
                    return False
                self._write(directory, old | {"status": "cancelled"}, now, previous=old)
                return True
        except Exception:
            return False
