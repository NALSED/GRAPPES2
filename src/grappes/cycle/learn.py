"""Apprentissage borné et recalibrage des seuils (FINAL.md § 8).

* UNIQUEMENT à partir des entités « confirmées » : recoupées (validation acquise) par des citants d'au
  moins `min_ecosystemes` écosystèmes. Jamais à partir d'entités « probables » seules ni d'une source
  mono-écosystème.
* Termes discriminants (fréquents chez les confirmées, rares chez les non-membres), ajoutés au
  « lexique_appris » séparé : poids faible, plafonné par cycle et au total, expiration si non revus.
  Les lexiques et ancres écrits par Sednal ne sont jamais lus en écriture.
* Garde-fou contre la contamination : un enseignant doit descendre d'une RACINE DE CONFIANCE (une
  ancre, ou une source trouvée par la recherche) par des citations fermes, 2 pas au plus. Un cercle de
  sources fausses qui se citent entre elles, sans lien depuis une racine, n'enseigne rien.
* Recalibrage borné de `seuil_min` et `marge_ambiguite`, calculé sur les confirmées seulement.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from grappes.amorcage import tokens
from grappes.autonomy import Autonomy
from grappes.clustering.assign import AssignResult
from grappes.config import Config
from grappes.learned import LearnedTerm
from grappes.models import Grappe
from grappes.semantic.scoring import normalize

TRUST_HOPS = 2


@dataclass(slots=True)
class GrappeLearning:
    grappe_id: str
    n_teachers: int = 0
    n_confirmed: int = 0
    n_untrusted: int = 0  # confirmées écartées faute de racine de confiance
    added: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    note: str = ""
    terms: dict[str, LearnedTerm] = field(default_factory=dict)  # nouvel état du lexique appris


def confirmed_by_grappe(result: AssignResult, auto: Autonomy) -> dict[str, set[str]]:
    """grappe -> références des entités confirmées (recoupement validé, >= N écosystèmes citants)."""
    out: dict[str, set[str]] = defaultdict(set)
    for rec in result.validations:
        if rec.validated and len(set(rec.ecosystems)) >= auto.min_ecosystemes:
            out[rec.grappe_id].add(rec.cited)
    return out


def trusted_refs(
    result: AssignResult, grappes: tuple[Grappe, ...], origins: dict[str, str]
) -> set[str]:
    """Racines (ancres de Sednal, sources trouvées par la recherche) et ce qu'elles citent,
    jusqu'à `TRUST_HOPS` pas, par des citations fermes (les hypothèses ne comptent pas)."""
    roots = {a for g in grappes for a in g.anchors} | {
        r for r, o in origins.items() if o == "recherche"
    }
    out_edges: dict[str, set[str]] = defaultdict(set)
    for c in result.citations:
        if not c.hypothesis:
            out_edges[c.citer].add(c.cited)
    trusted, frontier = set(roots), set(roots)
    for _ in range(TRUST_HOPS):
        frontier = {y for x in frontier for y in out_edges.get(x, ())} - trusted
        trusted |= frontier
    return trusted


def _docs(observations) -> dict[str, set[str]]:
    texts: dict[str, list[str]] = defaultdict(list)
    for o in observations:
        texts[o.source.key].append(o.text)
    return {ref: set(tokens(" ".join(ts))) for ref, ts in texts.items()}


def _known_terms(g: Grappe) -> set[str]:
    """Termes déjà dans le lexique, y compris chaque mot d'un terme composé (« café crème »)."""
    known: set[str] = set()
    for t in (*g.lexicon.terms, *g.lexicon.negative):
        n = normalize(t).lstrip("#")
        known.add(n)
        known.update(w for w in re.split(r"[^\w-]+", n) if w)
    return known


def learn_grappe(
    g: Grappe,
    result: AssignResult,
    observations,
    confirmed: set[str],
    trusted: set[str],
    existing: dict[str, LearnedTerm],
    auto: Autonomy,
    cycle: int,
    members_of_g: set[str],
) -> GrappeLearning:
    res = GrappeLearning(g.grappe_id, n_confirmed=len(confirmed), terms=dict(existing))
    docs = _docs(observations)
    teachers = {
        r for r in confirmed if r in docs and (r in trusted or not auto.exiger_racine_de_confiance)
    }
    res.n_teachers = len(teachers)
    res.n_untrusted = len([r for r in confirmed if r in docs]) - len(teachers)
    others = [
        r for r in docs if r not in teachers and r not in members_of_g
    ]  # « ailleurs » = non-membres
    if len(teachers) < auto.min_confirmees:
        res.note = f"{len(teachers)} entité(s) confirmée(s) fiable(s) < {auto.min_confirmees} : rien appris"
        stats_ok: dict[str, float] = {}
    else:
        n_in, n_out = len(teachers), len(others)
        df_in: dict[str, int] = defaultdict(int)
        df_out: dict[str, int] = defaultdict(int)
        for r in teachers:
            for t in docs[r]:
                df_in[t] += 1
        for r in others:
            for t in docs[r]:
                df_out[t] += 1
        stats_ok = {}
        for t, c in df_in.items():
            support = c / n_in
            rarity = (df_out[t] / n_out) if n_out else 0.0
            if c >= 2 and support >= auto.support_min and rarity <= auto.rarete_max:
                stats_ok[t] = support * math.log((n_out + 1) / (df_out[t] + 1) + 1e-9) + support
    # termes déjà appris : revus (toujours discriminants) ou expirés
    for t, lt in existing.items():
        if t in stats_ok:
            res.terms[t] = LearnedTerm(lt.weight, lt.added, cycle)
            res.kept.append(t)
        elif cycle - lt.seen >= auto.expiration_cycles:
            res.terms.pop(t)
            res.expired.append(t)
    # nouveaux termes : les plus discriminants, plafonnés par cycle et au total
    known = _known_terms(g)
    fresh = sorted(
        (t for t in stats_ok if t not in known and t not in res.terms),
        key=lambda t: (-stats_ok[t], t),
    )
    for t in fresh[: auto.max_termes_par_cycle]:
        res.terms[t] = LearnedTerm(auto.poids_appris, cycle, cycle)
        res.added.append(t)
    while (
        len(res.terms) > auto.max_termes_total
    ):  # plafond total : on retire les plus anciens non revus
        victim = min(res.terms, key=lambda t: (res.terms[t].seen, res.terms[t].added, t))
        res.terms.pop(victim)
        if victim in res.added:
            res.added.remove(victim)
        else:
            res.expired.append(victim)
    return res


# --- seuil d'ingestion auto-ajustable ----------------------------------------------------------------------

MIN_INGESTED = 3  # en dessous, trop peu de sources pour juger de la pertinence
PRECISION_LOW = 1 / 3  # part de sources ingérées qui sont rangées : en dessous, on durcit
PRECISION_HIGH = 0.8  # au-dessus, et si beaucoup de candidats sont refusés, on assouplit
REFUSED_SHARE_HIGH = 0.2


@dataclass(slots=True)
class IngestionAdjust:
    before: float
    after: float
    reason: str

    @property
    def changed(self) -> bool:
        return abs(self.after - self.before) > 1e-9


def adjust_ingestion(
    auto: Autonomy,
    ingested_refs: list[str],
    classed_refs: set[str],
    n_examined: int,
    n_refused_proximity: int,
) -> IngestionAdjust:
    """Ajuste `seuil_ingestion` dans ses bornes, d'au plus `variation_max` par cycle, d'après la
    pertinence observée : parmi les sources ingérées ce cycle, quelle part est ensuite rangée dans un sujet ?

    * moins d'un tiers : trop de bruit entre -> seuil relevé ;
    * plus de 80 % ET au moins 20 % des résultats examinés refusés pour proximité insuffisante : on rate
      sans doute de bonnes sources -> seuil abaissé ;
    * sinon, ou si moins de 3 sources ingérées : inchangé.
    Retourne la décision et sa raison (reprises dans le rapport de cycle).
    """
    lo, hi, now = auto.seuil_ingestion_min, auto.seuil_ingestion_max, auto.seuil_ingestion
    keep = IngestionAdjust(now, now, "")
    if not auto.seuil_ingestion_auto:
        keep.reason = "ajustement automatique non autorisé (`auto_ajustable` faux)"
        return keep
    refs = list(dict.fromkeys(ingested_refs))
    if len(refs) < MIN_INGESTED:
        keep.reason = f"{len(refs)} source(s) ingérée(s) < {MIN_INGESTED} : trop peu pour juger de la pertinence"
        return keep
    kept = sum(1 for r in refs if r in classed_refs)
    precision = kept / len(refs)
    refused = n_refused_proximity / n_examined if n_examined else 0.0
    step = auto.variation_max
    if precision < PRECISION_LOW:
        new = min(now + step, hi)
        why = f"seulement {kept} source(s) sur {len(refs)} ingérée(s) rangée(s) dans un sujet ({precision:.0%}) : trop de bruit"
    elif precision > PRECISION_HIGH and refused >= REFUSED_SHARE_HIGH:
        new = max(now - step, lo)
        why = (
            f"{kept} source(s) sur {len(refs)} rangée(s) ({precision:.0%}) et {refused:.0%} des résultats refusés "
            "pour proximité insuffisante : des sources pertinentes sont probablement écartées"
        )
    else:
        keep.reason = f"pertinence satisfaisante ({kept} sur {len(refs)} rangée(s), {refused:.0%} refusés pour proximité)"
        return keep
    if abs(new - now) < 1e-9:
        keep.reason = f"{why} ; mais le seuil est déjà à sa borne ({now:g})"
        return keep
    return IngestionAdjust(now, round(new, 2), why)


# --- recalibrage borné -----------------------------------------------------------------------------


@dataclass(slots=True)
class Recalibration:
    before: dict[str, float]
    after: dict[str, float]
    note: str = ""

    @property
    def changed(self) -> bool:
        return any(abs(self.after[k] - self.before[k]) > 1e-9 for k in self.after)


def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def recalibrate(
    result: AssignResult, cfg: Config, auto: Autonomy, confirmed: dict[str, set[str]]
) -> Recalibration:
    """`seuil_min` et `marge_ambiguite`, ramenés vers une cible calculée sur les confirmées seulement,
    d'au plus `variation_max` par cycle et toujours dans les bornes configurées.

    Avec les seules confirmées (aucun exemple négatif connu), on ne peut mesurer qu'une chose : le seuil
    exclut-il des entités sûres ? Le recalibrage ne fait donc que RELÂCHER un seuil trop sévère (jamais
    le durcir) : cible = 5 points sous le 10e centile de la confiance des confirmées ; marge = moitié du
    25e centile de leur avance sur la grappe suivante."""
    sc = cfg.scoring
    before = {"seuil_min": sc.seuil_min, "marge_ambiguite": sc.marge_ambiguite}
    rec = Recalibration(before, dict(before))
    if not auto.recalibrage_actif:
        rec.note = "recalibrage désactivé"
        return rec
    ents = {e.entity_id: e.primary_ref for e in result.entities}
    by_ref: dict[str, list[dict]] = defaultdict(list)
    for m in result.memberships:
        by_ref[ents[m["entity_id"]]].append(m)
    own, gaps = [], []
    for gid, refs in confirmed.items():
        for r in refs:
            ms = sorted(by_ref.get(r, []), key=lambda m: -m["confidence"])
            mine = next((m for m in ms if m["grappe_id"] == gid), None)
            if mine is None:
                continue
            own.append(mine["confidence"])
            others = [m["confidence"] for m in ms if m["grappe_id"] != gid]
            if others:
                gaps.append(mine["confidence"] - max(others))
    if len(own) < auto.min_confirmees:
        rec.note = f"{len(own)} entité(s) confirmée(s) < {auto.min_confirmees} : seuils inchangés"
        return rec
    targets = {"seuil_min": _percentile(own, 0.10) - 5.0}
    if gaps:
        targets["marge_ambiguite"] = 0.5 * _percentile([max(g, 0.0) for g in gaps], 0.25)
    bounds = {"seuil_min": auto.bornes_seuil_min, "marge_ambiguite": auto.bornes_marge}
    for key, target in targets.items():
        lo, hi = bounds[key]
        target = min(max(target, lo), hi)
        if target >= before[key]:
            continue  # le seuil actuel n'exclut pas de confirmée : on ne le durcit pas
        step = max(-auto.variation_max, target - before[key])
        rec.after[key] = round(min(max(before[key] + step, lo), hi), 2)
    return rec
