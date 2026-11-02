"""Classement d'un sujet, jugement et mesure : tout se fait depuis les données archivées (zéro réseau, sauf
Wikidata pour juger un nom jamais vu, mis en cache).

Méthodes de classement (`Params.methode`) :
* `actuel` : le système tel qu'il était avant la mission — confiance de `assign`, entités rangées au niveau
  « probable » ou plus, nom lisible. Base de comparaison n° 1.
* `vote` : vote majoritaire (`MajorityVoting`) sur les citations. Base de comparaison n° 2.
* `sums`, `averagelog`, `investment`, `pooledinvestment`, `truthfinder` : découverte de vérité.

Score continu d'une entité : somme pondérée normalisée de S (proximité sémantique du texte propre, 0 à 1), de la
croyance de découverte de vérité (0 à 1) et du facteur de fiabilité des sources (Admiralty/Tranco/registres, 0 à 1).
"""

from __future__ import annotations

from dataclasses import dataclass

from grappes.autocal import td
from grappes.autocal.juger import FAUX, JUSTE, NON_VERIFIABLE, Juge
from grappes.autocal.metriques import evaluer
from grappes.classement.candidats import Contexte
from grappes.canon import site_key


@dataclass(frozen=True, slots=True)
class Params:
    methode: str = "actuel"
    w_s: float = 1.0
    w_t: float = 0.0
    w_f: float = 0.0
    negatifs: bool = False
    g: float = 1.2  # investment, pooledinvestment
    rho: float = 0.5  # truthfinder : influence_param
    gamma: float = 0.3  # truthfinder : dampening_factor
    editeur_unique: bool = True  # une voix par éditeur (D58), comme dans le recoupement actuel
    nom_requis: bool = True
    registres: tuple[str, ...] = ()  # registres de découverte utilisés (wikidata, sirene) ; vide = système d'avant D63
    variantes: bool = True  # variantes de noms regroupées par nomenklatura avant la notation (méthode « lr »)
    seuil_variantes: float = 0.8
    lien: str = "simple"  # regroupement des variantes : « simple » (historique, enchaîne) ou « complet » (toutes les paires au seuil)
    lr: tuple[tuple[str, float], ...] = ()  # surcharges des valeurs de config/lr.yaml (réglage Optuna)
    ner: bool = False  # entités nommées dans le texte (chantier 2, D63) ajoutées aux candidats et aux citations
    w_rw: float = 0.0  # poids de l'appartenance à Wikidata (classe du sujet) dans le score
    w_rs: float = 0.0  # poids de l'appartenance à SIRENE/RNA (existence, pas appartenance au sujet : D51)

    def td_params(self) -> dict:
        if self.methode in ("investment", "pooledinvestment"):
            return {"g": self.g}
        if self.methode == "truthfinder":
            return {"influence_param": self.rho, "dampening_factor": self.gamma}
        return {}


@dataclass(slots=True)
class Classe:
    ref: str
    nom: str
    score: float  # 0 à 1
    s: float = 0.0
    croyance: float = 0.0
    facteur: float = 0.0


def _voix(ctx: Contexte, unique: bool) -> list[tuple[str, str]]:
    """Citations (citant, cité), un seul citant par éditeur et par entité citée si `unique` (D58)."""
    if not unique:
        return ctx.citations
    seen: dict[tuple[str, str], str] = {}
    for citer, cited in ctx.citations:
        eco, _, ident = citer.partition(":")
        key = (site_key(ident) if eco in ("web", "rss") else citer, cited)
        seen.setdefault(key, citer)
    return sorted((v, k[1]) for k, v in seen.items())


def _confiance_initiale(ctx: Contexte, citants: set[str]) -> dict[str, float]:
    """Confiance initiale d'une source dans ]0, 1[ : f/(f+1) du facteur de fiabilité existant (Admiralty, Tranco,
    registres) — transformation monotone, aucun barème nouveau."""
    return {c: ctx.rel.factor(c) / (ctx.rel.factor(c) + 1.0) for c in citants}


def classer(ctx: Contexte, p: Params) -> list[Classe]:
    """Classement complet décroissant (ex æquo : ordre alphabétique de la clé, déterministe)."""
    base = ctx.candidats
    if p.registres:
        from grappes.autocal.registres_pool import fusionner

        base = fusionner(ctx, ctx.decouverts, p.registres)
    extra_cits: list[tuple[str, str]] = []
    if p.ner:
        from grappes.autocal.ner_pool import ajouter

        base, extra_cits = ajouter(ctx, base, ctx.nommes)
    cands = [c for c in base if (c.nom is not None or not p.nom_requis)]
    if p.methode == "lr":
        from grappes import vraisemblance as vr
        from grappes.autocal import indice

        cfg_lr = vr.charger().avec(**dict(p.lr))
        if p.variantes:
            cands = indice.fusion_variantes(ctx, cands, p.seuil_variantes, p.lien)
        rows = []
        for c in cands:
            lg = vr.total(indice.elements(ctx, c, cfg_lr), cfg_lr)
            rows.append(Classe(c.ref, c.nom or c.nom_brut, 1.0 / (1.0 + 10 ** (-lg)), c.s / 100.0, lg, 0.0))
        return sorted(rows, key=lambda r: (-r.score, r.ref))
    if p.methode == "actuel":
        seuil = (
            ctx.cfg.scoring.niveau_probable
            if ctx.cfg.scoring.resultat_confiance_min is None
            else ctx.cfg.scoring.resultat_confiance_min
        )
        rows = [
            Classe(c.ref, c.nom or c.nom_brut, c.confiance / 100.0, c.s / 100.0)
            for c in cands
            if c.exportable_actuel and c.confiance >= seuil
        ]
    else:
        cits = _voix(ctx, p.editeur_unique)
        cits = sorted(set(cits) | set(extra_cits))
        belief: dict[str, float] = {}
        if cits:
            citants = {s for s, _ in cits}
            belief = td.croyances(
                cits, p.methode, _confiance_initiale(ctx, citants), lues=ctx.lues, negatifs=p.negatifs,
                **p.td_params(),
            )  # fmt: skip
        wsum = (p.w_s + p.w_t + p.w_f + p.w_rw + p.w_rs) or 1.0
        rows = []
        for c in cands:
            b = belief.get(c.ref, 0.0)
            f = c.facteur / (c.facteur + 1.0)
            rows.append(
                Classe(
                    c.ref,
                    c.nom or c.nom_brut,
                    (p.w_s * c.s / 100.0 + p.w_t * b + p.w_f * f + p.w_rw * ("wikidata" in c.registres) + p.w_rs * ("sirene" in c.registres)) / wsum,
                    c.s / 100.0,
                    b,
                    f,
                )
            )
        if p.methode == "vote":
            pass
    return sorted(rows, key=lambda r: (-r.score, r.ref))


@dataclass(slots=True)
class Jugement:
    classe: Classe
    verdict: str
    docid: str
    raison: str


def juger_classement(juge: Juge, rows: list[Classe]) -> list[Jugement]:
    out = []
    for r in rows:
        v = juge.juger(r.nom, r.ref)
        out.append(Jugement(r, v.verdict, v.docid, v.raison))
    return out


def run_de(sujet_id: str, jug: list[Jugement], n: int | None = None) -> dict[str, dict[str, float]]:
    """Run TREC : docid -> score, doublons (même QID) fusionnés par le meilleur score, au plus `n` documents."""
    docs: dict[str, float] = {}
    for j in jug:
        docs[j.docid] = max(docs.get(j.docid, 0.0), j.classe.score)
    top = sorted(docs.items(), key=lambda kv: (-kv[1], kv[0]))[: n or None]
    return {sujet_id: dict(top)}


def qrels_de(ref, jug: list[Jugement]) -> dict[str, dict[str, int]]:
    """Référence (1) + faux jugés (0). Un résultat juste hors de la liste initiale (classe correcte trouvée par
    libellé) est ajouté à la référence : il est vérifié, pas supposé."""
    q = {e.qid: 1 for e in ref.entities}
    for j in jug:
        if j.verdict == FAUX:
            q.setdefault(j.docid, 0)
        elif j.verdict == JUSTE:
            q.setdefault(j.docid, 1)
    return {ref.sujet_id: q}


def comptes(jug: list[Jugement], n: int | None = None) -> dict[str, int]:
    """Trois comptes (jamais deux) sur les `n` premiers résultats distincts."""
    seen: set[str] = set()
    c = {JUSTE: 0, FAUX: 0, NON_VERIFIABLE: 0}
    for j in jug:
        if j.docid in seen:
            continue
        seen.add(j.docid)
        if n is not None and len(seen) > n:
            break
        c[j.verdict] += 1
    return c


def rprec_ex_aequo(run: dict[str, float], pertinents: set[str], r: int) -> float:
    """R-précision ESPÉRÉE quand les ex æquo sont départagés au hasard (valeur exacte, hypergéométrique) : le résultat ne
    dépend plus de l'ordre alphabétique des clés. Les documents strictement au-dessus du score de frontière comptent
    entièrement ; les ex æquo de la frontière se partagent les places restantes à parts égales."""
    if r <= 0 or not run:
        return 0.0
    scores = sorted(run.values(), reverse=True)
    frontiere = scores[min(r, len(scores)) - 1]
    dessus = [d for d, v in run.items() if v > frontiere]
    egaux = [d for d, v in run.items() if v == frontiere]
    places = min(r, len(scores)) - len(dessus)
    juste_dessus = sum(1 for d in dessus if d in pertinents)
    juste_egaux = sum(1 for d in egaux if d in pertinents)
    return (juste_dessus + places * juste_egaux / len(egaux)) / r


def mesurer(ref, jug: list[Jugement]) -> dict:
    qrels = qrels_de(ref, jug)
    run = run_de(ref.sujet_id, jug)
    m = evaluer(qrels, run)[ref.sujet_id]
    r = sum(1 for v in qrels[ref.sujet_id].values() if v > 0)
    pert = {d for d, v in qrels[ref.sujet_id].items() if v > 0}
    m["Rprec_ex_aequo"] = rprec_ex_aequo(run[ref.sujet_id], pert, r)
    return {
        "mesures": m,
        "R": r,
        "comptes": comptes(jug),
        "comptes_R": comptes(jug, r),
        "n": len(run[ref.sujet_id]),
    }
