"""Hybrid retrieval: lexical BM25 and embeddings, merged by reciprocal rank.

In memory and deterministic. Embeddings come from the ``embedder`` model role; tests and
offline runs use :class:`HashingEmbedder` (character n-grams hashed into a fixed vector).
"""

import hashlib
import logging
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

log = logging.getLogger(__name__)

#: Function words of the deployment languages (es, ca-valencia, en): not evidence of a match.
STOPWORDS = frozenset(
    [
        "a",
        "al",
        "an",
        "and",
        "at",
        "by",
        "de",
        "del",
        "dels",
        "des",
        "el",
        "els",
        "en",
        "es",
        "et",
        "for",
        "from",
        "i",
        "in",
        "la",
        "las",
        "le",
        "les",
        "lo",
        "los",
        "me",
        "my",
        "of",
        "on",
        "or",
        "per",
        "please",
        "por",
        "pour",
        "que",
        "the",
        "to",
        "un",
        "una",
        "uno",
        "y",
        "we",
        "you",
        "acompana",
        "acompanya",
        "acompaname",
        "acompanyam",
        "lleva",
        "llevame",
        "porta",
        "portam",
        "take",
        "bring",
        "show",
        "can",
        "could",
        "would",
        "puedes",
        "pots",
        "quiero",
        "vull",
        "necesito",
        "necessite",
        "need",
        "want",
        "going",
        "go",
        "ir",
        "anar",
        "where",
        "donde",
        "on",
        "esta",
        "està",
        "is",
        "are",
        "visitor",
        "visitante",
        "visitant",
        "persona",
        "person",
    ]
)


def fold(text: str) -> str:
    """Lower case without accents: "Parada de Autobús" -> "parada de autobus"."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def tokens(text: str, *, keep_stopwords: bool = False) -> list[str]:
    words = re.findall(r"[a-z0-9]+", fold(text))
    return words if keep_stopwords else [w for w in words if w not in STOPWORDS]


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic offline embedder: character 3-grams of folded words, hashed."""

    def __init__(self, dims: int = 256) -> None:
        self.dims = dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = np.zeros(self.dims)
            for word in tokens(text):
                padded = f"#{word}#"
                for i in range(max(1, len(padded) - 2)):
                    gram = padded[i : i + 3]
                    h = int.from_bytes(hashlib.sha1(gram.encode()).digest()[:4], "little")
                    vec[h % self.dims] += 1.0
            norm = np.linalg.norm(vec)
            out.append((vec / norm if norm else vec).tolist())
        return out


class ModelEmbedder:
    """The ``embedder`` role (LangChain ``init_embeddings``), memoised per text."""

    def __init__(self, embeddings: Any) -> None:
        self._embeddings = embeddings
        self._cache: dict[str, list[float]] = {}

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            for text, vec in zip(missing, self._embeddings.embed_documents(missing), strict=True):
                self._cache[text] = vec
        return [self._cache[t] for t in texts]


@dataclass
class Entry:
    id: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


class HybridIndex:
    """BM25 + cosine over embeddings, fused with reciprocal-rank fusion (k = 60)."""

    def __init__(self, entries: Sequence[Entry], embedder: Embedder | None = None) -> None:
        self.entries = list(entries)
        self.embedder = embedder
        self._docs = [tokens(e.text) for e in self.entries]
        self._df = Counter(w for d in self._docs for w in set(d))
        self._avg = sum(len(d) for d in self._docs) / max(1, len(self._docs))
        self._vecs = (
            np.array(embedder.embed([e.text for e in self.entries]))
            if embedder is not None and self.entries
            else None
        )

    def bm25(self, query: str, k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
        q = tokens(query)
        n = len(self._docs)
        scores = []
        for i, doc in enumerate(self._docs):
            tf = Counter(doc)
            s = 0.0
            for w in q:
                if w not in tf:
                    continue
                idf = math.log(1 + (n - self._df[w] + 0.5) / (self._df[w] + 0.5))
                s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(doc) / self._avg))
            if s > 0:
                scores.append((i, s))
        return sorted(scores, key=lambda x: (-x[1], self.entries[x[0]].id))

    def dense(self, query: str) -> list[tuple[int, float]]:
        if self._vecs is None or self.embedder is None:
            return []
        try:
            q = np.array(self.embedder.embed([query])[0])
        except Exception as exc:  # the embedder failed mid-run: lexical only
            log.warning("embedder failed (%s): BM25 only", exc)
            return []
        sims = self._vecs @ q / (np.linalg.norm(self._vecs, axis=1) * (np.linalg.norm(q) or 1.0))
        order = sorted(range(len(sims)), key=lambda i: (-sims[i], self.entries[i].id))
        return [(i, float(sims[i])) for i in order if sims[i] > 0]

    def search(self, query: str, k: int = 5, rrf_k: int = 60) -> list[tuple[Entry, float]]:
        fused: dict[int, float] = {}
        for ranking in (self.bm25(query), self.dense(query)):
            for rank, (i, _) in enumerate(ranking):
                fused[i] = fused.get(i, 0.0) + 1.0 / (rrf_k + rank + 1)
        order = sorted(fused, key=lambda i: (-fused[i], self.entries[i].id))
        return [(self.entries[i], round(fused[i], 6)) for i in order[:k]]
