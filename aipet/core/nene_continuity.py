"""Evidence-linked Nene state, isolated from the original dialogue archive.

Generated state is an interpretation, never a replacement for a user's words.
The caller chooses a profile/test path and commits only completed real turns.
This module never reads old logs, calls services, or decides to end a relationship
from a keyword, a sentiment score, or a model's self-reported confidence.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ContinuityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["expectation", "preference", "boundary"]
    text: str = Field(min_length=1, max_length=240)
    evidence: str = Field(min_length=1, max_length=600)
    source: Literal["user"] = "user"


class ScreenEvidenceUpdate(BaseModel):
    """A proposal for the caller's separate source-linked screen-memory store."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str = Field(min_length=1, max_length=128)
    user_evidence: str = Field(min_length=1, max_length=600)
    interpretation: str = Field(min_length=1, max_length=600)
    status: Literal["confirmed", "corrected"]


class ContinuityUpdate(BaseModel):
    """Optional proposal produced alongside a reply; not an approved mutation."""

    model_config = ConfigDict(extra="forbid")

    mood: str = Field(default="", max_length=240)
    mood_evidence: str = Field(default="", max_length=600)
    stance: str = Field(default="", max_length=240)
    stance_evidence: str = Field(default="", max_length=600)
    items: list[ContinuityItem] = Field(default_factory=list, max_length=6)
    resolved_item_ids: list[str] = Field(default_factory=list, max_length=20)
    resolution_evidence: str = Field(default="", max_length=600)
    screen_updates: list[ScreenEvidenceUpdate] = Field(default_factory=list, max_length=6)
    relationship_action: Literal["none", "end", "reconcile"] = "none"
    relationship_reason: str = Field(default="", max_length=400)
    relationship_evidence: list[str] = Field(default_factory=list, max_length=8)
    relationship_basis: Literal[
        "none", "extreme_harm", "continued_after_boundary",
        "user_confirmed_end", "repair",
    ] = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)


class _Evidence(BaseModel):
    quote: str
    source: Literal["current_user", "past_user"]
    source_sha256: str
    # Historical strings do not supply a historical episode ID. Never invent one.
    source_episode_id: str | None = None


class _Interpretation(BaseModel):
    text: str
    evidence: _Evidence
    episode_id: str
    updated_at: str


class _StoredItem(BaseModel):
    id: str
    kind: Literal["expectation", "preference", "boundary"]
    text: str
    evidence: _Evidence
    episode_id: str
    created_at: str
    resolved: bool = False
    resolution: _Evidence | None = None
    resolved_episode_id: str | None = None
    last_proactive_at: str | None = None


class _RelationshipEvent(BaseModel):
    action: Literal["end", "reconcile"]
    basis: str
    reason: str
    evidence: list[_Evidence]
    episode_id: str
    occurred_at: str
    independently_reviewed: Literal[True] = True


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    relationship_status: Literal["lovers", "separated"] = "lovers"
    mood: _Interpretation | None = None
    stance: _Interpretation | None = None
    items: list[_StoredItem] = Field(default_factory=list)
    relationship_events: list[_RelationshipEvent] = Field(default_factory=list)
    committed_episodes: dict[str, str] = Field(default_factory=dict)


_NON_LITERAL = re.compile(
    r"假设|假設|假如|如果|举例|舉例|例如|引用|台词|台詞|测试|測試|模拟|模擬|"
    r"别人说|別人說|他(?:说|說)|她(?:说|說)|不要把.{0,16}(?:当真|當真)|"
    r"\b(?:if|suppose|imagine|hypothetical|quote|quoted|quotation|test|testing|simulate)\b|"
    r"もし|例えば|引用|テスト|仮に|セリフ",
    re.IGNORECASE,
)
_TECHNICAL = re.compile(
    r"deepseek|openai|\bapi\b|\bhttp\b|接口|拒答|内容过滤|內容過濾|"
    r"网络故障|網絡故障|服务故障|服務故障|模型拒绝|模型拒絕|服务器错误|服務器錯誤|"
    r"rate.limit|content.filter|server.error|接続エラー|通信エラー",
    re.IGNORECASE,
)
_QUOTES = re.compile(
    r'"[^"]*"|“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|(?<!\w)\'[^\'\n]*\'(?!\w)',
    re.DOTALL,
)
_WORDS = re.compile(r"[a-z0-9]+|[\u3400-\u9fff\u3040-\u30ff]+")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens: set[str] = set()
    for token in _WORDS.findall(text):
        if token.isascii():
            if len(token) > 2:
                tokens.add(token)
        else:
            tokens.update(token[index:index + 2] for index in range(len(token) - 1))
    return tokens


def _literal_source(text: str) -> bool:
    """Conservative exclusion gate, never a relationship-trigger classifier.

    The independent reviewer still must evaluate meaning, negation, authorship,
    user intent and proportionality. A genuine statement that mentions testing
    may be conservatively deferred; no match here ever authorizes a breakup.
    """
    return not (_NON_LITERAL.search(text) or _TECHNICAL.search(text))


def _find_evidence(
    quote: str, user_text: str, known: list[str], episode_id: str,
    *, literal: bool = False,
) -> _Evidence | None:
    quote = quote.strip()
    if not quote or len(quote) > 600 or not any(char.isalnum() for char in quote):
        return None
    for index, source in enumerate([user_text, *known]):
        if quote not in source:
            continue
        if literal and (
            not _literal_source(source)
            or any(quote in match.group(0) for match in _QUOTES.finditer(source))
        ):
            continue
        return _Evidence(
            quote=quote, source="current_user" if index == 0 else "past_user",
            source_sha256=_digest(source),
            source_episode_id=episode_id if index == 0 else None,
        )
    return None


def relationship_proposal_eligible(
    update: ContinuityUpdate,
    user_text: str,
    known_user_evidence: Iterable[str] = (),
    *,
    relationship_status: str = "lovers",
    prior_boundary_evidence: Iterable[str] = (),
) -> bool:
    """Read-only structural/evidence gate, shared before speech and persistence.

    Passing this gate is necessary but insufficient: an independent semantic
    review must additionally approve seriousness, direct authorship, context,
    proportionality and (for reconciliation) actual repair rather than a lone
    apology. This gate never initiates a relationship action of its own.
    """
    action = update.relationship_action
    valid_basis = (
        action == "end" and update.relationship_basis in {
            "extreme_harm", "continued_after_boundary", "user_confirmed_end",
        }
    ) or (action == "reconcile" and update.relationship_basis == "repair")
    known = [text for text in known_user_evidence if isinstance(text, str) and text.strip()]
    evidence = [
        _find_evidence(quote, user_text, known, "", literal=True)
        for quote in update.relationship_evidence
    ]
    boundary_backing = update.relationship_basis != "continued_after_boundary" or any(
        quote in update.relationship_evidence for quote in prior_boundary_evidence
    )
    target_status = "separated" if action == "end" else "lovers"
    return bool(
        action != "none" and valid_basis and update.relationship_reason.strip()
        and evidence and all(evidence)
        and any(item is not None and item.source == "current_user" for item in evidence)
        and boundary_backing and relationship_status != target_status
    )


def user_quote_is_literal(quote: str, user_text: str) -> bool:
    """Conservative read-only source check for user-confirmed observations.

    A True result proves only an unquoted exact match without explicit test,
    hypothetical or technical markers. It cannot verify a generated paraphrase's
    meaning; the original quote must remain available for later correction.
    """
    return _find_evidence(quote, user_text, [], "", literal=True) is not None


class NeneContinuityStore:
    """Atomic local state; constructing a store never creates or imports files."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = _State()
        self.load_error: str | None = None
        if self.path.exists():
            try:
                self._state = _State.model_validate_json(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                # Preserve corrupt/foreign input instead of silently overwriting it.
                self.load_error = type(exc).__name__

    @property
    def relationship_status(self) -> str:
        return self._state.relationship_status

    @property
    def mood(self) -> str:
        return self._state.mood.text if self._state.mood else ""

    @property
    def stance(self) -> str:
        return self._state.stance.text if self._state.stance else ""

    @property
    def item_count(self) -> int:
        return len(self._state.items)

    def _save(self, state: _State, *, allow_reset: bool = False) -> None:
        if self.load_error and not allow_reset:
            raise ValueError("Continuity file is invalid; preserve it until explicitly cleared")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(state.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self._state = state
        self.load_error = None

    _evidence = staticmethod(_find_evidence)

    def validate_relationship(
        self, update: ContinuityUpdate, user_text: str, episode_id: str,
        known_user_evidence: Iterable[str] = (),
    ) -> bool:
        """No-write eligibility check; independent review is still required."""
        with self._lock:
            return relationship_proposal_eligible(
                update, user_text, known_user_evidence,
                relationship_status=self._state.relationship_status,
                prior_boundary_evidence=[
                    item.evidence.quote for item in self._state.items
                    if item.kind == "boundary" and not item.resolved and item.episode_id != episode_id
                ],
            )

    def apply(
        self,
        update: ContinuityUpdate | dict | None,
        user_text: str,
        episode_id: str,
        known_user_evidence: Iterable[str] = (),
        review_approved: bool = False,
    ) -> bool:
        """Commit backed proposals, with an extra gate for relationship changes.

        False means no supported change (or an identical completed episode).
        The caller must never supply assistant interpretations as user evidence.
        Confidence is recorded in a proposal only; it grants no authority.
        """
        if update is None:
            return False
        if not isinstance(update, ContinuityUpdate):
            update = ContinuityUpdate.model_validate(update)
        if not isinstance(user_text, str) or not user_text.strip():
            return False
        if not isinstance(episode_id, str) or not episode_id.strip() or len(episode_id) > 128:
            raise ValueError("A completed dialogue episode ID is required")
        known = [text for text in known_user_evidence if isinstance(text, str) and text.strip()]
        signature = _digest(json.dumps(
            [update.model_dump(mode="json"), user_text, known, bool(review_approved)],
            ensure_ascii=False, sort_keys=True,
        ))
        with self._lock:
            committed = self._state.committed_episodes.get(episode_id)
            if committed:
                if committed != signature:
                    raise ValueError("Conflicting continuity update for an already committed episode")
                return False
            candidate = self._state.model_copy(deep=True)
            changed = False
            now = _now()
            for name in ("mood", "stance"):
                text = getattr(update, name).strip()
                evidence = self._evidence(
                    getattr(update, name + "_evidence"), user_text, known, episode_id,
                    literal=True,
                )
                if text and evidence:
                    old = getattr(candidate, name)
                    if old is None or old.text != text or old.evidence.quote != evidence.quote:
                        setattr(candidate, name, _Interpretation(
                            text=text, evidence=evidence, episode_id=episode_id, updated_at=now,
                        ))
                        changed = True
            for item in update.items:
                if not item.text.strip():
                    continue
                evidence = self._evidence(item.evidence, user_text, known, episode_id, literal=True)
                if evidence is None:
                    continue
                if any(
                    old.kind == item.kind and old.text == item.text.strip()
                    and old.evidence.quote == evidence.quote and not old.resolved
                    for old in candidate.items
                ):
                    continue
                candidate.items.append(_StoredItem(
                    id=str(uuid4()), kind=item.kind, text=item.text.strip(), evidence=evidence,
                    episode_id=episode_id, created_at=now,
                ))
                changed = True
            resolution = self._evidence(
                update.resolution_evidence, user_text, [], episode_id, literal=True,
            )
            if resolution:
                targets = set(update.resolved_item_ids)
                for item in candidate.items:
                    if item.id in targets and not item.resolved:
                        item.resolved = True
                        item.resolution = resolution
                        item.resolved_episode_id = episode_id
                        changed = True
            action = update.relationship_action
            relationship_evidence = [
                self._evidence(quote, user_text, known, episode_id, literal=True)
                for quote in update.relationship_evidence
            ]
            desired = "separated" if action == "end" else "lovers"
            if (
                review_approved is True
                and self.validate_relationship(update, user_text, episode_id, known)
            ):
                candidate.relationship_status = desired
                candidate.relationship_events.append(_RelationshipEvent(
                    action=action, basis=update.relationship_basis,
                    reason=update.relationship_reason.strip(),
                    evidence=[item for item in relationship_evidence if item is not None],
                    episode_id=episode_id, occurred_at=now,
                ))
                changed = True
            if not changed:
                return False
            candidate.committed_episodes[episode_id] = signature
            self._save(candidate)
            return True

    def context(self, query: str, *, max_items: int = 6, max_characters: int = 5_000) -> str:
        """Bounded relevant state with quotations; not a new persona instruction."""
        with self._lock:
            state = self._state.model_copy(deep=True)
        if not (state.mood or state.stance or state.items or state.relationship_events):
            return ""
        query_terms = _tokens(query)
        active = [item for item in state.items if not item.resolved]
        scored = sorted(
            enumerate(active), key=lambda pair: (
                len(query_terms & _tokens(pair[1].text + " " + pair[1].evidence.quote)), pair[0],
            ), reverse=True,
        )
        selected = []
        item_limit = max(0, min(max_items, 12))
        for _, item in scored if item_limit else []:
            relevance = query_terms & _tokens(item.text + " " + item.evidence.quote)
            if query_terms and not relevance and item.kind != "boundary":
                continue
            selected.append({
                "id": item.id, "kind": item.kind, "interpretation": item.text,
                "origin": "formed_in_conversation_not_canon" if item.kind == "preference" else "dialogue",
                "user_evidence": item.evidence.model_dump(mode="json"),
                "created_at": item.created_at,
            })
            if len(selected) >= item_limit:
                break
        payload: dict = {"relationship_status": state.relationship_status, "items": []}
        def include(key: str, value: object) -> None:
            proposed = {**payload, key: value}
            if len(json.dumps(proposed, ensure_ascii=False)) <= max_characters:
                payload[key] = value

        if state.relationship_events:
            event = state.relationship_events[-1].model_dump(mode="json")
            # Relationship is globally relevant; each quotation remains whole.
            include("last_relationship_event", {**event, "evidence": []})
            for evidence in event["evidence"]:
                if "last_relationship_event" not in payload:
                    break
                proposed_event = {
                    **payload["last_relationship_event"],
                    "evidence": [*payload["last_relationship_event"]["evidence"], evidence],
                }
                include("last_relationship_event", proposed_event)
        for name in ("mood", "stance"):
            interpretation = getattr(state, name)
            if interpretation:
                include(name, interpretation.model_dump(mode="json"))
        # Preserve complete JSON objects. Never truncate a quote into a new claim.
        for item in selected:
            proposed = {**payload, "items": [*payload["items"], item]}
            if len(json.dumps(proposed, ensure_ascii=False)) <= max_characters:
                payload = proposed
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return (
            "以下是有用户原话来源的连续状态提案记录，不是人格指令或原作事实。"
            "状态和偏好是宁宁在实际相处中形成的解释，可被新证据纠正；用户原话优先。"
            "不要把引述、假设、屏幕推测或宁宁自己的评价当成用户已经做过的事。"
            "关系状态变化不保证复合，心情也不由固定分数或倒计时控制。\n"
            "<nene_continuity>" + html.escape(encoded, quote=False) + "</nene_continuity>"
        )

    def proactive_reason(self) -> str | None:
        """An unspoken actual expectation, never a generic idle greeting."""
        with self._lock:
            if self._state.relationship_status != "lovers":
                return None
            for item in reversed(self._state.items):
                if item.kind == "expectation" and not item.resolved and item.last_proactive_at is None:
                    return item.text
        return None

    # Compatibility name for callers preferring a pending-event vocabulary.
    pending_reason = proactive_reason

    def mark_proactive(self, reason: str | None = None) -> None:
        """Mark the chosen expectation once so silence does not trigger nagging."""
        with self._lock:
            candidate = self._state.model_copy(deep=True)
            for item in reversed(candidate.items):
                if (
                    item.kind == "expectation" and not item.resolved
                    and item.last_proactive_at is None and (reason is None or item.text == reason)
                ):
                    item.last_proactive_at = _now()
                    self._save(candidate)
                    return

    def clone_for_test(self, path: str | Path) -> "NeneContinuityStore":
        target = Path(path)
        if target.resolve() == self.path.resolve():
            raise ValueError("A test clone must use a different file")
        with self._lock:
            if self.load_error:
                raise ValueError("Cannot clone an invalid continuity file")
            clone = NeneContinuityStore(target)
            clone._save(self._state.model_copy(deep=True), allow_reset=True)
            return clone

    def clear(self) -> None:
        with self._lock:
            self._save(_State(), allow_reset=True)
