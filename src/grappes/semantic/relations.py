"""Ancres et oppositions (MISSION §9.4)."""

from __future__ import annotations

from collections.abc import Iterable

from grappes.ecosystem.crossref import Citation
from grappes.models import Grappe


def anchor_scores(
    grappe: Grappe, refs: Iterable[str], citations: list[Citation]
) -> dict[str, float]:
    """A(e, g) : 100 pour une ancre ; sinon 100 × min(1, n_liens_ancres / 2), où un lien est
    une ancre distincte qui cite e ou qui est citée par e."""
    anchors = set(grappe.anchors)
    linked: dict[str, set[str]] = {}
    for c in citations:
        if c.citer in anchors and c.cited not in anchors:
            linked.setdefault(c.cited, set()).add(c.citer)
        if c.cited in anchors and c.citer not in anchors:
            linked.setdefault(c.citer, set()).add(c.cited)
    out: dict[str, float] = {}
    for ref in refs:
        if ref in anchors:
            out[ref] = 100.0
        else:
            out[ref] = 100.0 * min(1.0, len(linked.get(ref, ())) / 2)
    return out


def are_opposed(a: Grappe, b: Grappe) -> bool:
    """Opposition déclarée d'un côté ou de l'autre (symétrique)."""
    return b.grappe_id in a.oppositions or a.grappe_id in b.oppositions


def is_ambiguous(
    best: Grappe,
    confidences: dict[str, float],
    grappes: dict[str, Grappe],
    margin: float,
) -> bool:
    """Ambigu si une grappe opposée à `best` a une confiance ≥ conf(best) − marge, ou si
    n'importe quelle autre grappe est à moins de `margin`. Citer un opposant ne prouve rien."""
    top = confidences[best.grappe_id]
    for gid, conf in confidences.items():
        if gid == best.grappe_id or conf <= 0:
            continue
        if top - conf < margin or (are_opposed(best, grappes[gid]) and conf >= top - margin):
            return True
    return False
