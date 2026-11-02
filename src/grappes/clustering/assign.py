"""Score de confiance et affectation aux grappes (MISSION §10.2, §11).

E vaut 0 tant que le recoupement n'est pas validé (D19).
Formule par défaut (D15) : confiance = S + (100 − S)·(0,6·E + 0,4·A) / 100 ; sans citations,
confiance = S. Option `formule_confiance: ponderee` : w_s·S + w_e·E + w_a·A (D12).
Amorçage (itération 0) : même formule avec E = 0, pour désigner les membres initiaux
(ancres comprises). Ensuite jusqu'à `max_iter` itérations : les citants de l'itération n
sont les membres de n−1.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from grappes.config import Config, ScoringConfig
from grappes.ecosystem.crossref import (
    Citation,
    ValidationRecord,
    build_citations,
    filter_window,
    validate_crossref,
)
from grappes.fiabilite import Reliability
from grappes.models import Entity, EntityType, Observation
from grappes.personnes import is_institution
from grappes.semantic.relations import anchor_scores, are_opposed, is_ambiguous
from grappes.semantic.scoring import score_proximity

NON_CLASSE = "non_classé"
CLASSE = "classé"
AMBIGU = "ambigu"
SECONDAIRE = "secondaire"


@dataclass(slots=True)
class EntityStats:
    ecosystems: set[str] = field(default_factory=set)
    n_observations: int = 0
    citing: set[str] = field(default_factory=set)
    first_seen: datetime | None = None
    last_seen: datetime | None = None

    def see(self, ts: datetime | None) -> None:
        if ts is None:
            return
        if self.first_seen is None or ts < self.first_seen:
            self.first_seen = ts
        if self.last_seen is None or ts > self.last_seen:
            self.last_seen = ts


@dataclass(slots=True)
class AssignResult:
    entities: list[Entity]
    memberships: list[dict]
    stats: dict[str, EntityStats]  # clé = entity_id
    citations: list[Citation]
    validations: list[ValidationRecord] = field(default_factory=list)
    iterations: int = 0
    names: dict[str, str] = field(default_factory=dict)  # clé de source -> nom lisible connu
    admiralty: dict[str, str] = field(
        default_factory=dict
    )  # clé de source -> « B2 » (fiabilité, crédibilité)


def entity_id_for(primary_ref: str) -> str:
    return "E-" + hashlib.sha1(primary_ref.encode()).hexdigest()[:12]


def level(confidence: float, cfg: Config, validated: bool = True) -> str:
    """confirmé / probable / faible. « Confirmé » exige un recoupement validé (FINAL.md § 7) : une
    source non recoupée reste « probable » au mieux, quelle que soit sa confiance sémantique."""
    if confidence >= cfg.scoring.niveau_confirme and validated:
        return "confirmé"
    if confidence >= cfg.scoring.niveau_confirme:
        return "probable"
    if confidence >= cfg.scoring.niveau_probable:
        return "probable"
    return "faible"


def combine(s: float, e: float, a: float, sc: ScoringConfig) -> float:
    """Confiance à partir de S (sémantique), E (recoupement) et A (ancrage), tous sur 0 à 100."""
    if sc.formule_confiance == "ponderee":
        return sc.w_semantic * s + sc.w_ecosystem * e + sc.w_anchor * a
    return s + (100.0 - s) * (sc.c_ecosystem * e + sc.c_anchor * a) / 100.0


def _top_mean(scores: list[float], n: int) -> float:
    best = sorted(scores, reverse=True)[:n]
    return round(sum(best) / len(best), 2) if best else 0.0


def credibility(
    status: str, validated: bool, n_member_citers: int, opposed_conflict: bool, confidence: float
) -> int:
    """Crédibilité Admiralty (1 à 6) d'une appartenance, déduite des signaux du recoupement — aucun barème :
    1 confirmée (recoupement validé) ; 2 probable : rangée ET citée par un membre ; 3 possible : rangée d'après son
    seul texte ; 4 douteuse : signal sous le seuil, ou ambiguïté par la marge ; 5 improbable : ambiguïté avec une
    grappe OPPOSÉE (contredite) ; 6 non jugeable : aucun signal."""
    if validated:
        return 1
    if status == CLASSE:
        return 2 if n_member_citers >= 1 else 3
    if status == AMBIGU:
        return 5 if opposed_conflict else 4
    return 4 if confidence > 0 else 6


def assign(
    observations: list[Observation],
    cfg: Config,
    names: dict[str, str] | None = None,
    reliability: Reliability | None = None,
) -> AssignResult:
    rel = reliability or Reliability(cfg.fiabilite)
    citations = build_citations(observations, names)
    texts_own: dict[str, list[Observation]] = defaultdict(list)
    for o in observations:
        texts_own[o.source.key].append(o)
    obs_by_id = {o.obs_id: o for o in observations}
    texts_cited: dict[str, list[Observation]] = defaultdict(list)
    stats_by_ref: dict[str, EntityStats] = defaultdict(EntityStats)

    for ref, obs_list in texts_own.items():
        st = stats_by_ref[ref]
        st.ecosystems.add(ref.split(":", 1)[0])
        for o in obs_list:
            st.n_observations += 1
            st.see(o.ts)
    for c in citations:
        st = stats_by_ref[c.cited]
        st.ecosystems.add(c.cited.split(":", 1)[0])
        st.citing.add(c.citer)
        texts_cited[c.cited].append(obs_by_id[c.obs_id])
        if c.cited not in texts_own:
            st.see(c.ts)

    refs = sorted(stats_by_ref)
    grappes = {g.grappe_id: g for g in cfg.grappes}
    sc = cfg.scoring

    # poids d'une voix = poids de la source (sources.yaml) × facteur de fiabilité Admiralty (D50)
    def weight(ref: str) -> float:
        return cfg.sources.weight(ref) * rel.factor(ref)

    # S(e, g) : calculé une fois ; texte propre, à défaut contexte des citations (§9.2)
    sem: dict[tuple[str, str], float] = {}
    for ref in refs:
        # texte propre ; à défaut, contexte des citations — seulement si `contexte_min_citants` est défini
        # et atteint. Par défaut JAMAIS : un site jamais lu (google.com, spotify.com... cités en pied de
        # page) ne doit pas être rangé d'après le texte de ceux qui le citent ; seul le recoupement peut
        # classer une entité sans texte propre (premières données réelles, D45).
        enough = (
            sc.contexte_min_citants is not None
            and len(stats_by_ref[ref].citing) >= sc.contexte_min_citants
        )
        context = texts_own.get(ref) or (texts_cited.get(ref, []) if enough else [])
        for g in cfg.grappes:
            kw = {**g.lexicon.terms, **{k: -v for k, v in g.lexicon.negative.items()}}
            sem[ref, g.grappe_id] = _top_mean(
                [score_proximity(o.text, kw, scale=g.scale) for o in context], sc.top_n_observations
            )
    firm = [c for c in citations if not c.hypothesis]  # D25 : une hypothèse ne valide rien
    # les ancres ne se lisent que dans les liens explicites : une mention textuelle n'ancre rien (D54)
    anc = {
        g.grappe_id: anchor_scores(g, refs, [c for c in firm if c.via != "texte"])
        for g in cfg.grappes
    }
    windowed = filter_window(firm, sc.crossref.window_days)

    # itération 0 : amorçage sans E
    ws, wa = sc.w_semantic, sc.w_anchor

    def bootstrap(s: float, a: float) -> float:
        if sc.formule_confiance == "ponderee":  # renormalisée sans E
            return (ws * s + wa * a) / (ws + wa) if ws + wa else 0.0
        return combine(s, 0.0, a, sc)

    conf: dict[tuple[str, str], float] = {
        (r, gid): bootstrap(sem[r, gid], anc[gid][r]) for r in refs for gid in grappes
    }
    eco: dict[tuple[str, str], float] = dict.fromkeys(conf, 0.0)
    via: dict[tuple[str, str], str] = {
        (r, gid): ("ancre" if r in grappes[gid].anchors else "semantique") for r, gid in conf
    }
    # membres connus (citants admissibles) : amorcés par l'itération 0, puis cumulatifs, pour
    # que la formule complète (E, A absents) ne fasse pas sortir un citant déjà reconnu.
    minc = sc.crossref.min_citer_confidence
    members: dict[str, set[str]] = {
        gid: {r for r in refs if r in g.anchors or conf[r, gid] >= minc}
        for gid, g in grappes.items()
    }
    validations: dict[tuple[str, str], ValidationRecord] = {}
    iterations = 0
    for _ in range(sc.max_iter):
        iterations += 1
        prev_members = {gid: set(m) for gid, m in members.items()}
        prev = conf
        validations = {}
        new_conf: dict[tuple[str, str], float] = {}
        new_eco: dict[tuple[str, str], float] = {}
        new_via: dict[tuple[str, str], str] = {}
        for gid, g in grappes.items():
            anchors = set(g.anchors)

            known = prev_members[gid]
            records = validate_crossref(
                windowed, gid, lambda ref, known=known: ref in known, weight, sc.crossref,
                lambda ref: cfg.sources.weight(ref) * rel.text_factor(ref),
            )  # fmt: skip
            for r in refs:
                rec = records.get(r)
                # E potentiel (citants membres) ; retenu seulement si le recoupement est validé
                # (D19) : un refus ne doit pas suffire à classer l'entité.
                e = (
                    100.0 * min(1.0, rec.weight_sum / (2 * sc.crossref.min_weight_sum))
                    if rec and rec.validated
                    else 0.0
                )
                c = combine(sem[r, gid], e, anc[gid][r], sc)
                v = "ancre" if r in anchors else "semantique"
                if rec is not None:
                    validations[r, gid] = rec
                    if rec.validated:
                        c, v = max(c, sc.crossref_floor), "recoupement"
                new_conf[r, gid], new_eco[r, gid], new_via[r, gid] = round(c, 2), round(e, 2), v
        for gid in grappes:
            members[gid] |= {r for r in refs if new_conf[r, gid] >= minc}
        converged = new_conf == prev and members == prev_members
        conf, eco, via = new_conf, new_eco, new_via
        if converged:
            break

    entities: list[Entity] = []
    admiralty: dict[
        str, str
    ] = {}  # clé de source -> « B2 » : fiabilité de la source, crédibilité de l'information
    memberships: list[dict] = []
    stats: dict[str, EntityStats] = {}
    for ref in refs:
        etype = EntityType(sc.entity_types.get(ref.split(":", 1)[0], "person"))
        if etype is EntityType.PERSON and is_institution(
            (names or {}).get(ref), sc.resultat_indices_institution
        ):
            etype = EntityType.GROUP  # compte institutionnel : indice explicite dans le nom
        eid = entity_id_for(ref)
        entities.append(Entity(eid, etype, ref, (names or {}).get(ref) or ref.split(":", 1)[1]))
        stats[eid] = stats_by_ref[ref]
        scored = sorted(((gid, conf[ref, gid]) for gid in grappes), key=lambda t: (-t[1], t[0]))
        best_id, best = scored[0]
        classed = best >= sc.seuil_min
        ambiguous = classed and is_ambiguous(
            grappes[best_id], dict(scored), grappes, sc.marge_ambiguite
        )
        opposed = ambiguous and any(
            gid != best_id
            and c > 0
            and are_opposed(grappes[best_id], grappes[gid])
            and c >= best - sc.marge_ambiguite
            for gid, c in scored
        )
        letter = rel.letter(ref)
        best_cred = 6
        for gid, c in scored:
            if c <= 0:
                continue
            if gid == best_id:
                status = (AMBIGU if ambiguous else CLASSE) if classed else NON_CLASSE
            else:
                status = SECONDAIRE
            rec = validations.get((ref, gid))
            cred = credibility(
                status,
                bool(rec and rec.validated),
                len(rec.citers) if rec else 0,
                opposed and gid == best_id,
                c,
            )
            if gid == best_id:
                best_cred = cred
            memberships.append(
                {
                    "fiabilite": letter,
                    "credibilite": cred,
                    "entity_id": eid,
                    "grappe_id": gid,
                    "semantic": sem[ref, gid],
                    "ecosystem": eco[ref, gid],
                    "anchor": round(anc[gid][ref], 2),
                    "confidence": c,
                    "via": via[ref, gid],
                    "status": status,
                }
            )
        admiralty[ref] = f"{letter}{best_cred}"
    cred_of = {ref: int(code[1]) for ref, code in admiralty.items()}
    citations = [
        dataclasses.replace(c, reliability=rel.letter(c.citer), credibility=cred_of.get(c.cited, 6))
        for c in citations
    ]
    ordered = [validations[k] for k in sorted(validations)]
    known = {r: n for r, n in (names or {}).items() if r in stats_by_ref and n}
    return AssignResult(
        entities, memberships, stats, citations, ordered, iterations, known, admiralty
    )
