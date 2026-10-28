"""The InteractionStore: the only place human-interaction content lives.

In memory only: nothing is written to disk, logged or backed up. Each entry has a TTL
(``interaction_ttl_min``) and can be deleted explicitly. Only the coordinator and the
robots in the entry's ``participants`` can read it; the coordinator changes the
participants (hand-over). A robot that took part can re-create a lost entry from its own
copy (``restore``).

ACL stand-in: the caller is an agent id (a deployment makes identities real with mTLS).
"""

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from a6g_common.clock import Clock, SystemClock
from a6g_common.schemas import InteractionRef

COORDINATOR = "coordinator"


class InteractionNotFound(KeyError):
    """No entry with that id (never created, deleted, or expired)."""


class InteractionDenied(PermissionError):
    """The caller is neither the coordinator nor a participant."""


@dataclass
class _Entry:
    interaction_id: str
    participants: list[str]
    created: datetime
    expires: datetime
    content: dict[str, Any] = field(default_factory=dict)

    def ref(self) -> InteractionRef:
        return InteractionRef(
            interaction_id=self.interaction_id,
            participants=list(self.participants),
            created=self.created,
            expires=self.expires,
        )

    def view(self) -> dict[str, Any]:
        return {
            **self.ref().model_dump(mode="json"),
            "content": copy.deepcopy(self.content),
        }


class InteractionStore:
    def __init__(self, ttl_min: float = 60.0, clock: Clock | None = None) -> None:
        self.ttl = timedelta(minutes=ttl_min)
        self.clock = clock or SystemClock()
        self._entries: dict[str, _Entry] = {}

    def _expire(self) -> None:
        now = self.clock.now()
        for iid in [i for i, e in self._entries.items() if e.expires <= now]:
            del self._entries[iid]

    def _get(self, interaction_id: str, caller: str) -> _Entry:
        self._expire()
        entry = self._entries.get(interaction_id)
        if entry is None:
            raise InteractionNotFound(interaction_id)
        if caller != COORDINATOR and caller not in entry.participants:
            raise InteractionDenied(f"{caller} is not a participant of {interaction_id}")
        return entry

    def create(
        self,
        interaction_id: str,
        content: dict[str, Any],
        participants: list[str],
        caller: str,
    ) -> dict[str, Any]:
        """A robot opens an interaction it takes part in (or the coordinator, for anyone)."""
        self._expire()
        if caller != COORDINATOR and caller not in participants:
            raise InteractionDenied(f"{caller} may only create interactions it takes part in")
        if interaction_id in self._entries:
            raise ValueError(f"interaction {interaction_id} already exists")
        now = self.clock.now()
        entry = _Entry(interaction_id, list(participants), now, now + self.ttl, dict(content))
        self._entries[interaction_id] = entry
        return entry.view()

    def restore(self, entry: dict[str, Any], caller: str) -> dict[str, Any]:
        """Re-create a lost entry from a participant's copy: same id and
        participants, a fresh TTL. An entry still present is returned unchanged."""
        self._expire()
        iid = entry["interaction_id"]
        if iid in self._entries:
            return self._get(iid, caller).view()
        participants = list(entry.get("participants") or [])
        if caller != COORDINATOR and caller not in participants:
            raise InteractionDenied(f"{caller} did not take part in {iid}")
        now = self.clock.now()
        created = entry.get("created")
        restored = _Entry(
            iid,
            participants,
            datetime.fromisoformat(created) if isinstance(created, str) else now,
            now + self.ttl,
            dict(entry.get("content") or {}),
        )
        self._entries[iid] = restored
        return restored.view()

    def get(self, interaction_id: str, caller: str) -> dict[str, Any]:
        return self._get(interaction_id, caller).view()

    def update(
        self,
        interaction_id: str,
        caller: str,
        content: dict[str, Any] | None = None,
        participants: list[str] | None = None,
    ) -> dict[str, Any]:
        """Merge ``content``; only the coordinator changes the participants (hand-over)."""
        entry = self._get(interaction_id, caller)
        if participants is not None:
            if caller != COORDINATOR:
                raise InteractionDenied("only the coordinator changes the participants")
            if not participants:
                raise ValueError("an interaction needs at least one participant")
            entry.participants = list(participants)
        if content:
            entry.content.update(content)
        return entry.view()

    def delete(self, interaction_id: str, caller: str) -> bool:
        try:
            self._get(interaction_id, caller)
        except InteractionNotFound:
            return False
        del self._entries[interaction_id]
        return True

    def __len__(self) -> int:
        self._expire()
        return len(self._entries)

    def ids(self) -> list[str]:
        self._expire()
        return sorted(self._entries)

    def clear(self) -> None:
        """Lose every entry (tests: a store restart; nothing is persisted)."""
        self._entries.clear()
