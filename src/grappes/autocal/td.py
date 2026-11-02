"""Découverte de vérité (truth discovery, Yin, Han & Yu 2008) avec le paquet libre `truthdiscovery`.

Modélisation (DECISION_AUTOCAL.md, méthode 1) : une SOURCE est une source citante (celle qui contient le lien ou la
mention), une VARIABLE est « cette entité fait partie du sujet », la VALEUR affirmée est 1. Le vote majoritaire
(`MajorityVoting`) est la base de comparaison obligatoire ; les cinq autres algorithmes (Sums, AverageLog,
Investment, PooledInvestment, TruthFinder) estiment conjointement la confiance des sources et la croyance dans
chaque affirmation. Variante `negatifs` : une source LUE qui ne cite pas l'entité affirme 0 (hypothèse de monde
clos, discutable : mesurée par ablation, jamais supposée bonne).

Valeurs initiales (méthode 1) : la confiance initiale d'une source vient de ce qui existe déjà chez nous —
fiabilité Admiralty (D50), rang Tranco (D53), appariement aux registres (D51, D52) — via `Reliability.factor`.
TruthFinder accepte un vecteur `initial_trust` (une valeur par source). Pour les autres, la bibliothèque ne
propose que des croyances a priori énumérées : on utilise son point d'extension `get_prior_beliefs` (sous-classe
minimale, aucune réécriture d'algorithme) pour amorcer la croyance des affirmations par la confiance initiale
des sources qui les portent, comme l'amorçage de Sums/HITS.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterable, Mapping

import numpy as np
from numpy import ma

# `truthdiscovery` importe `cairo` pour ses graphiques (inutilisés ici, dépendance système absente) : module vide.
sys.modules.setdefault("cairo", types.ModuleType("cairo"))

from truthdiscovery import (
    AverageLog,
    Investment,
    MajorityVoting,
    MatrixDataset,
    PooledInvestment,
    Sums,
    TruthFinder,
)

ALGOS = {
    "vote": MajorityVoting,
    "sums": Sums,
    "averagelog": AverageLog,
    "investment": Investment,
    "pooledinvestment": PooledInvestment,
    "truthfinder": TruthFinder,
}


def _seeded(cls):
    """Sous-classe minimale : croyance a priori des affirmations = somme de la confiance initiale de leurs
    sources (normalisée). Point d'extension documenté de la bibliothèque, algorithme intact."""

    class Seeded(cls):  # type: ignore[valid-type, misc]
        trust0: np.ndarray | None = None

        def get_prior_beliefs(self, data):
            base = super().get_prior_beliefs(data)
            if self.trust0 is None:
                return base
            b = data.sc.T @ self.trust0
            return b / b.max() if b.max() > 0 else base

    Seeded.__name__ = cls.__name__
    return Seeded


def croyances(
    citations: Iterable[tuple[str, str]],
    algo: str = "sums",
    trust0: Mapping[str, float] | None = None,
    lues: Iterable[str] = (),
    negatifs: bool = False,
    **params,
) -> dict[str, float]:
    """Croyance (0 à 1) que chaque entité citée fait partie du sujet. `citations` : couples (citant, cité)."""
    pairs = sorted(set(citations))
    if not pairs:
        return {}
    sources = sorted({s for s, _ in pairs} | (set(lues) if negatifs else set()))
    cibles = sorted({c for _, c in pairs})
    si, ci = {s: i for i, s in enumerate(sources)}, {c: j for j, c in enumerate(cibles)}
    mat = np.full((len(sources), len(cibles)), np.nan)
    if negatifs:
        for s in lues:
            if s in si:
                mat[si[s], :] = 0.0
    for s, c in pairs:
        mat[si[s], ci[c]] = 1.0
    data = MatrixDataset(ma.masked_array(mat, np.isnan(mat)))
    t0 = (
        np.array([min(max(float(trust0.get(s, 0.5)), 0.01), 0.99) for s in sources])
        if trust0
        else None
    )
    cls = ALGOS[algo]
    if algo == "truthfinder":
        if t0 is not None:
            params = {**params, "initial_trust": t0}
        alg = cls(**params)
    elif algo == "vote":
        alg = cls()
    else:
        alg = _seeded(cls)(**params)
        alg.trust0 = t0
    res = alg.run(data)
    out: dict[str, float] = {}
    for j, c in enumerate(cibles):
        claims = res.belief.get(j, {})
        yes = float(claims.get(1.0, 0.0))
        no = float(claims.get(0.0, 0.0))
        out[c] = yes / (yes + no) if (yes + no) > 0 else 0.0
    return out
