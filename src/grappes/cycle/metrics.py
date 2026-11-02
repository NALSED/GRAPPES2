"""Indicateurs de stabilité d'un run (lus dans la base) et décision de retour en arrière."""

from __future__ import annotations

from dataclasses import dataclass, field

from grappes.autonomy import Autonomy
from grappes.config import Config
from grappes.storage.db import Database


@dataclass(slots=True)
class Metrics:
    run_id: str | None = None
    n_entities: int = 0
    n_classed: int = 0  # entités assignées à une grappe (classées ou ambiguës)
    n_ambiguous: int = 0
    n_confirmed: int = 0  # confirmées : recoupement validé et confiance >= niveau_confirme
    main: dict[str, str] = field(default_factory=dict)  # clé de source -> grappe principale

    @property
    def ambiguity_rate(self) -> float:
        return self.n_ambiguous / self.n_classed if self.n_classed else 0.0

    @property
    def confirmed_rate(self) -> float:
        return self.n_confirmed / self.n_classed if self.n_classed else 0.0


def run_metrics(db: Database, run_id: str | None, cfg: Config) -> Metrics:
    """Métriques d'un run enregistré. `None` si le run n'existe pas (premier cycle)."""
    m = Metrics(run_id=run_id)
    if run_id is None:
        return m
    m.n_entities = db.conn.execute(
        "SELECT COUNT(DISTINCT entity_id) FROM memberships WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    rows = db.conn.execute(
        "SELECT e.primary_ref, ms.grappe_id, ms.confidence, ms.via, ms.status FROM memberships ms"
        " JOIN entities e USING(entity_id) WHERE ms.run_id=? AND ms.status IN ('classé','ambigu')"
        " ORDER BY e.primary_ref, ms.grappe_id",
        (run_id,),
    ).fetchall()
    for r in rows:  # une seule appartenance « principale » par entité (classé ou ambigu)
        m.n_classed += 1
        m.main[r["primary_ref"]] = r["grappe_id"]
        if r["status"] == "ambigu":
            m.n_ambiguous += 1
        if r["via"] == "recoupement" and r["confidence"] >= cfg.scoring.niveau_confirme:
            m.n_confirmed += 1
    return m


@dataclass(slots=True)
class Indicators:
    changed_share: float = 0.0  # part des entités classées avant ET après qui changent de grappe
    ambiguity_rate: float = 0.0
    confirmed_rate: float = 0.0
    confirmed_drop: float = 0.0  # baisse relative du taux de confirmés (0 si hausse)
    lexicon_drift: float = 0.0  # termes appris / termes de base (Sednal + amorçage)
    exceeded: list[str] = field(default_factory=list)  # indicateurs au-delà de leur seuil


def compare(
    before: Metrics, after: Metrics, auto: Autonomy, n_learned: int, n_base: int
) -> Indicators:
    both = [r for r in after.main if r in before.main]
    changed = sum(1 for r in both if before.main[r] != after.main[r])
    ind = Indicators(
        changed_share=changed / len(both) if both else 0.0,
        ambiguity_rate=after.ambiguity_rate,
        confirmed_rate=after.confirmed_rate,
        confirmed_drop=(
            max(0.0, (before.confirmed_rate - after.confirmed_rate) / before.confirmed_rate)
            if before.confirmed_rate > 0
            else 0.0
        ),
        lexicon_drift=(n_learned / n_base) if n_base else (float("inf") if n_learned else 0.0),
    )
    if (
        after.n_classed < auto.min_entites
    ):  # trop peu d'entités pour que les taux veuillent dire quelque chose
        return ind
    if ind.changed_share > auto.max_changement_grappe:
        ind.exceeded.append("changement_de_grappe")
    if ind.ambiguity_rate > auto.max_taux_ambiguite:
        ind.exceeded.append("taux_ambiguite")
    if ind.confirmed_drop > auto.chute_max_confirmes:
        ind.exceeded.append("chute_des_confirmes")
    if ind.lexicon_drift > auto.max_derive_lexique:
        ind.exceeded.append("derive_du_lexique")
    return ind


LABELS = {
    "changement_de_grappe": "trop d'entités changent de grappe",
    "taux_ambiguite": "trop d'entités ambiguës",
    "chute_des_confirmes": "chute de la part d'entités confirmées",
    "derive_du_lexique": "le lexique appris dérive trop par rapport au lexique de base",
}
