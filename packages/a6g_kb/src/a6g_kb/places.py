"""Place resolution (``kb_resolve_place``, SPEC A3.3.3): what a person or an operator calls a
place -> up to three campus candidates, in the deployment languages (es, ca-valencia, en).

Each candidate is scored on its name, aliases and short id against the text: whole-phrase
containment, fuzzy word coverage (typos; longer names said in full rank higher) and, with
an embedder, semantic similarity.
Accent-insensitive. Deterministic: ties go to points (POIs, places) before sectors, then id.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import numpy as np
from a6g_common.config import CampusConfig

from a6g_kb.index import Embedder, tokens

log = logging.getLogger(__name__)

_RANK = {"poi": 0, "place": 0, "sector": 1}


@dataclass(frozen=True)
class Place:
    kind: str  # poi | place | sector
    id: str
    name: str
    names: tuple[str, ...]  # name, aliases, short id


@dataclass
class Candidate:
    kind: str
    id: str
    name: str
    score: float
    matched: str
    scores: dict[str, float] = field(default_factory=dict)


def campus_places(campus: CampusConfig) -> list[Place]:
    out = []
    for s in campus.sectors:
        short = s.sector_id.split("_", 1)[-1].replace("_", " ")
        out.append(Place("sector", s.sector_id, s.name or short, (s.name, *s.aliases, short)))
    for p in campus.pois:
        out.append(Place("poi", p.poi_id, p.name or p.poi_id, (p.name, *p.aliases)))
    for r in campus.link_recovery_points:
        out.append(Place("place", r.point_id, r.label, (r.label, *r.aliases)))
    return [Place(p.kind, p.id, p.name, tuple(dict.fromkeys(n for n in p.names if n))) for p in out]


def _contained(name: list[str], text: list[str]) -> bool:
    n = len(name)
    return n > 0 and any(text[i : i + n] == name for i in range(len(text) - n + 1))


def _coverage(name: list[str], text: list[str]) -> float:
    """How much of the name the text says, word by word, tolerating typos (0..1)."""
    if not name or not text:
        return 0.0
    best = []
    for w in name:
        r = max(SequenceMatcher(None, w, t).ratio() for t in text)
        best.append(r if r >= 0.75 else 0.0)
    return sum(best) / len(best)


class PlaceResolver:
    def __init__(self, places: Sequence[Place], embedder: Embedder | None = None) -> None:
        self.places = list(places)
        self.embedder = embedder
        self._vecs: dict[tuple[str, str], np.ndarray] = {}
        if embedder is not None:
            names = [(p.id, n) for p in self.places for n in p.names]
            for key, vec in zip(names, embedder.embed([n for _, n in names]), strict=True):
                self._vecs[key] = np.array(vec)

    def resolve(self, text: str, min_score: float, k: int = 3) -> list[Candidate]:
        words = tokens(text)
        qvec = None
        if self.embedder is not None and words:
            try:
                qvec = np.array(self.embedder.embed([text])[0])
            except Exception as exc:  # the embedder failed mid-run: names and typos only
                log.warning("embedder failed (%s): lexical place resolution", exc)
        found = []
        for p in self.places:
            best: Candidate | None = None
            for n in p.names:
                nw = tokens(n)
                if not nw:
                    continue
                exact = 0.7 + 0.3 * len(nw) / max(len(words), 1) if _contained(nw, words) else 0.0
                # Typos tolerated; a longer name that the text says is more specific
                # ("marquesina del 9B" over "9B").
                fuzzy = 0.75 * _coverage(nw, words) + 0.05 * min(1.0, len(nw) / max(len(words), 1))
                dense = 0.0
                if qvec is not None and (p.id, n) in self._vecs:
                    v = self._vecs[(p.id, n)]
                    den = float(np.linalg.norm(v) * np.linalg.norm(qvec)) or 1.0
                    dense = 0.7 * float(v @ qvec) / den
                score = round(max(exact, fuzzy, dense), 4)
                if best is None or score > best.score:
                    best = Candidate(
                        p.kind,
                        p.id,
                        p.name,
                        score,
                        n,
                        {"exact": exact, "fuzzy": fuzzy, "dense": dense},
                    )
            if best is not None and best.score >= min_score:
                found.append(best)
        found.sort(key=lambda c: (-c.score, -len(tokens(c.matched)), _RANK[c.kind], c.id))
        return found[:k]
