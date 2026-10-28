"""Id generation. Inject an :class:`IdFactory` wherever determinism matters (tests, sim)."""

import itertools
import uuid
from typing import Protocol


class IdFactory(Protocol):
    def __call__(self, prefix: str) -> str: ...


def new_id(prefix: str) -> str:
    """Random, globally unique id such as ``dec_3f2a9c1b7d4e``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class SequentialIds:
    """Deterministic ids (``task_0001``, ``task_0002``...) per prefix."""

    def __init__(self) -> None:
        self._counters: dict[str, itertools.count[int]] = {}

    def __call__(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}_{next(counter):04d}"
