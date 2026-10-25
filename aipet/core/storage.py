"""Shared conversation and screen-event persistence."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import tempfile
import time
from uuid import uuid4
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from aipet.core.config import get_user_data_dir
from aipet.core.conversation_memory import ConversationMemory


_WINDOWS_REPLACE_RETRY = os.name == "nt"
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16)
_REPLACE_RETRY_BUDGET = 0.5


def _replace_storage_file(temporary: Path, destination: Path) -> None:
    """Retry only Windows access/sharing conflicts within a short budget.

    Access denied may also be permanent: retries are bounded and the original
    OSError is propagated when they cannot complete the replacement.
    """
    deadline = time.monotonic() + _REPLACE_RETRY_BUDGET
    for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
        try:
            # Path.replace delegates to os.replace and preserves the existing
            # error-injection boundary used by persistence regression tests.
            temporary.replace(destination)
            return
        except OSError as error:
            if (not _WINDOWS_REPLACE_RETRY or getattr(error, "winerror", None) not in {5, 32, 33}
                    or attempt == len(_REPLACE_RETRY_DELAYS)):
                raise
            delay = _REPLACE_RETRY_DELAYS[attempt]
            if time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Commit complete JSON using a closed, unique temporary in the same dir.

    Serialization finishes before touching the filesystem. Independent writers
    cannot overwrite each other's temporary data; the existing last-successful-
    replacement semantics remain, without a lock or a read/merge operation.
    """
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".aipet-storage-", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_storage_file(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A cleanup error must not replace the actual save failure.
                pass


class ScreenMemoryEntry(BaseModel):
    observation_id: str = Field(default_factory=lambda: uuid4().hex, max_length=64)
    source: Literal["screen_observation"] = "screen_observation"
    status: Literal["unconfirmed", "confirmed", "corrected"] = "unconfirmed"
    interpretation: str = Field(default="", max_length=1_200)
    user_evidence: str = Field(default="", max_length=1_200)
    confirmed_by_episode: str = Field(default="", max_length=64)
    occurred_at: str = Field(max_length=40)
    software: str = Field(default="", max_length=120)
    activity: str = Field(default="", max_length=1_200)
    topic: str = Field(default="", max_length=500)
    change_summary: str = Field(min_length=1, max_length=1_200)

    @classmethod
    def now(cls, **values) -> "ScreenMemoryEntry":
        return cls(
            occurred_at=datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
            **values,
        )

    def event_key(self) -> tuple[object, ...]:
        return (
            self.software.casefold(),
            self.activity.casefold(),
            self.topic.casefold(),
            self.change_summary.casefold(),
        )


class ScreenMemoryStore:
    def __init__(self, path: Path | None = None, limit: int = 12):
        self.path = path or (get_user_data_dir() / "screen_memory.json")
        self.limit = max(1, limit)
        self.entries = self.load()

    def load(self) -> list[ScreenMemoryEntry]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        events = payload.get("events", [])
        if not isinstance(events, list):
            return []
        valid: list[ScreenMemoryEntry] = []
        for event in events:
            try:
                valid.append(ScreenMemoryEntry.model_validate(event))
            except ValidationError:
                continue
        return valid[-self.limit :]

    def remember(self, entry: ScreenMemoryEntry) -> bool:
        if (
            self.entries
            and self.entries[-1].event_key() == entry.event_key()
        ):
            return False
        self.entries.append(entry)
        self.entries = self.entries[-self.limit :]
        self.save()
        return True

    def save(self) -> None:
        payload = {
            "events": [
                entry.model_dump(mode="json") for entry in self.entries
            ]
        }
        _atomic_write_json(self.path, payload)

    def prompt_text(
        self,
        *,
        max_entries: int = 8,
        max_characters: int = 2_400,
    ) -> str:
        selected: list[dict] = []
        used = 2
        for entry in reversed(self.entries[-max_entries:]):
            payload = entry.model_dump(mode="json")
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            extra = len(encoded) + (1 if selected else 0)
            if selected and used + extra > max_characters:
                break
            selected.append(payload)
            used += extra
        if not selected:
            return ""
        selected.reverse()
        return json.dumps(
            selected,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def clear(self) -> None:
        self.entries.clear()
        self.save()

    def confirm(self, updates, *, user_text: str, episode_id: str) -> bool:
        """An observation never establishes intention without user evidence."""
        from aipet.core.nene_continuity import user_quote_is_literal
        changed = False
        by_id = {entry.observation_id: entry for entry in self.entries}
        for update in updates:
            data = update.model_dump() if hasattr(update, "model_dump") else update
            if not isinstance(data, dict):
                continue
            entry = by_id.get(data.get("observation_id"))
            evidence = data.get("user_evidence", "")
            interpretation = data.get("interpretation", "")
            status = data.get("status")
            if (entry is None or not isinstance(evidence, str) or not evidence.strip()
                    or evidence not in user_text or len(evidence) > 1_200
                    or not isinstance(interpretation, str) or not interpretation.strip()
                    or len(interpretation) > 1_200 or status not in {"confirmed", "corrected"}
                    or not episode_id):
                continue
            if not user_quote_is_literal(evidence, user_text):
                continue
            entry.status, entry.interpretation = status, interpretation
            entry.user_evidence, entry.confirmed_by_episode = evidence, episode_id
            changed = True
        if changed:
            self.save()
        return changed


class HistoryStore:
    def __init__(
        self, path: Path | None = None, limit: int = 30,
        *, separate_proactive: bool = False,
    ):
        self.path = path or (get_user_data_dir() / "history.json")
        self.limit = max(4, limit)
        self.separate_proactive = separate_proactive
        self._pending_proactive: dict[str, str] | None = None
        self._conversation_memory = ConversationMemory()

    @property
    def conversation_count(self) -> int:
        return self._conversation_memory.count if self.separate_proactive else 0

    def remember_exchange(self, **values) -> str:
        if not self.separate_proactive:
            raise ValueError("Nene conversation memory is not enabled")
        return self._conversation_memory.remember(**values)

    def conversation_context(
        self, query: str, recent_history: list[dict[str, str]],
    ) -> str:
        if not self.separate_proactive:
            return ""
        return self._conversation_memory.recall(query, recent_history)

    def set_separate_proactive(self, enabled: bool) -> None:
        if self.separate_proactive != enabled:
            self._pending_proactive = None
        self.separate_proactive = enabled

    def record_proactive(self, content: str, event_context: str = "") -> None:
        if not self.separate_proactive:
            raise ValueError("Separate proactive history is not enabled")
        if not isinstance(content, str) or not 0 < len(content) <= 16_000:
            raise ValueError("Invalid proactive reply content")
        if not isinstance(event_context, str):
            event_context = ""
        self._pending_proactive = {
            "role": "assistant", "content": content,
            "source": "proactive_event", "event_context": event_context[:8_000],
            "occurred_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def pop_proactive(self) -> dict[str, str] | None:
        pending = self._pending_proactive if self.separate_proactive else None
        self._pending_proactive = None
        return dict(pending) if pending is not None else None

    def context_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        context = list(messages[-self.limit :])
        if self.separate_proactive and self._pending_proactive is not None:
            # This extra slot does not consume the genuine dialogue budget.
            context.append({**self._pending_proactive, "pending_proactive": "1"})
        return context

    def load(self) -> list[dict[str, str]]:
        self._pending_proactive = None
        self._conversation_memory = ConversationMemory()
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        if not isinstance(payload, dict):
            return []
        # Only this explicit field in the authoritative file can supply past
        # exchanges. Never infer episodes from legacy messages or read backups.
        records = payload.get("nene_conversation_memory", [])
        if isinstance(records, list):
            self._conversation_memory = ConversationMemory(records)
        pending = payload.get("nene_pending_proactive")
        if (
            self.separate_proactive and isinstance(pending, dict)
            and pending.get("role") == "assistant"
            and pending.get("source") == "proactive_event"
            and isinstance(pending.get("content"), str)
            and 0 < len(pending["content"]) <= 16_000
            and isinstance(pending.get("event_context", ""), str)
            and len(pending.get("event_context", "")) <= 8_000
        ):
            self._pending_proactive = {
                "role": "assistant", "content": pending["content"],
                "source": "proactive_event",
                "event_context": pending.get("event_context", ""),
            }
            occurred_at = pending.get("occurred_at")
            if isinstance(occurred_at, str) and len(occurred_at) <= 40:
                try:
                    datetime.fromisoformat(occurred_at)
                except ValueError:
                    pass
                else:
                    self._pending_proactive["occurred_at"] = occurred_at
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            return []
        valid = [
            message
            for message in messages
            if isinstance(message, dict)
            and message.get("role") in {"user", "assistant"}
            and isinstance(message.get("content"), str)
        ]
        return valid[-self.limit :]

    def save(self, messages: list[dict[str, str]]) -> None:
        payload = {"messages": messages[-self.limit :]}
        # Retain a loaded archive while another profile is active, but never
        # recall or append it for that profile. Both memory layers commit once.
        if self._conversation_memory.count:
            payload["nene_conversation_memory"] = self._conversation_memory.to_records()
        if self.separate_proactive and self._pending_proactive is not None:
            payload["nene_pending_proactive"] = self._pending_proactive
        _atomic_write_json(self.path, payload)

    def clear(self) -> None:
        self._pending_proactive = None
        self._conversation_memory.clear()
        self.save([])

