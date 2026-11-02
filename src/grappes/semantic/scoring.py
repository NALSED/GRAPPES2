"""Score de proximité sémantique d'un texte à un lexique (0 à 100)."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol


def normalize(text: str) -> str:
    """casefold + retrait des accents LATINS uniquement (cyrillique, arménien... intacts)."""
    t = unicodedata.normalize("NFKD", text.casefold())
    out: list[str] = []
    for ch in t:
        if unicodedata.combining(ch) and out and ord(out[-1]) < 0x250:
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))  # recompose й, etc.


@lru_cache(maxsize=4096)
def _pattern(kw_norm: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(kw_norm)}(?!\w)")


@dataclass(frozen=True, slots=True)
class ScoreDetail:
    score: float
    matched: dict[str, int]


def score_detail(
    text: str, keywords: Sequence[str] | Mapping[str, float], *, scale: float = 3.0
) -> ScoreDetail:
    """raw = Σ poids × (1 + ln tf) ; score = 100 × (1 − exp(−raw/scale)). Poids < 0 : pénalité."""
    items = keywords.items() if isinstance(keywords, Mapping) else ((k, 1.0) for k in keywords)
    haystack = normalize(text)
    raw = 0.0
    matched: dict[str, int] = {}
    for kw, w in items:
        k = normalize(kw).strip()
        if not k:
            continue
        tf = len(_pattern(k).findall(haystack))
        if tf:
            matched[kw] = tf
            raw += w * (1.0 + math.log(tf))
    raw = max(raw, 0.0)
    return ScoreDetail(round(100.0 * (1.0 - math.exp(-raw / scale)), 2), matched)


def score_proximity(
    text: str, keywords: Sequence[str] | Mapping[str, float], *, scale: float = 3.0
) -> float:
    return score_detail(text, keywords, scale=scale).score


class Scorer(Protocol):
    """Interface de scoring : `score_proximity` est l'implémentation par défaut (embeddings plus tard)."""

    def __call__(
        self, text: str, keywords: Sequence[str] | Mapping[str, float], *, scale: float = 3.0
    ) -> float: ...
