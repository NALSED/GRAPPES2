"""Candidats issus des liens déjà cités dans les données (D24) : aucun appel externe."""

from __future__ import annotations

from grappes.search.models import Candidate
from grappes.storage.db import Database

PROVIDER = "liens_internes"
_URL = {
    "telegram": "https://t.me/{}",
    "x": "https://x.com/{}",
    "instagram": "https://www.instagram.com/{}/",
    "web": "https://{}",
}


def internal_candidates(db: Database, max_citers: int = 20) -> list[Candidate]:
    """Éléments cités par des sources déjà ingérées mais jamais vus comme sources eux-mêmes.

    S'appuie sur les citations du dernier run (`grappes run`). Chaque candidat garde la liste de
    ses citants (`extra.citants`) : c'est la condition « citée par une source confirmée » de
    l'autonomie (phase 9).
    """
    run_id = db.latest_run_id()
    if run_id is None:
        return []
    observed = {r[0] for r in db.conn.execute("SELECT DISTINCT source_key FROM observations")}
    rows = db.conn.execute(
        "SELECT cited, citer FROM citations WHERE run_id=? ORDER BY cited, citer", (run_id,)
    ).fetchall()
    citers: dict[str, list[str]] = {}
    for cited, citer in rows:
        if cited not in observed and citer not in citers.setdefault(cited, []):
            citers[cited].append(citer)
    out = []
    for key, who in sorted(citers.items()):
        eco, ident = key.split(":", 1)
        out.append(
            Candidate(
                ecosystem=eco,
                ident=ident,
                url=_URL.get(eco, "").format(ident),
                keyword="",
                provider=PROVIDER,
                reason=f"citée par {len(who)} source(s) déjà ingérée(s)",
                extra={"n_citants": len(who), "citants": who[:max_citers]},
            )
        )
    return out
