"""Génération reproductible des sujets d'évaluation (graine notée) et des trois jeux disjoints.

Un sujet = une classe d'organisations Wikidata comptant 20 à 200 instances françaises : on obtient d'un coup une
phrase de sujet, une liste attendue (référence silver standard) et R pour la R-précision. Difficulté
(facile / moyen / difficile) = présence des acteurs dans Wikidata et dans les registres : part d'instances avec site
officiel (P856) et avec identifiant SIREN (P1616), répartie en terciles. Les trois jeux (mise au point, validation,
test) ont la même proportion de difficultés ; chacun garde un contrôle négatif (aucun acteur réel).
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from grappes.autocal.reference import FR, Reference, build, class_where
from grappes.autocal.wikidata import Wikidata, WikidataError

SEED = 20261012  # graine de tirage (notée ici et dans calibration.md)
ORG = "Q43229"  # organisation

CLASSES_QUERY = (
    "SELECT ?class (COUNT(?item) AS ?n) WHERE { ?item wdt:P856 ?site ; wdt:P17 wd:Q142 ; wdt:P31 ?class . } "
    "GROUP BY ?class HAVING (COUNT(?item) >= 20 && COUNT(?item) <= 250)"
)


@dataclass(slots=True)
class Sujet:
    id: str
    phrase: str
    jeu: str = ""  # dev | validation | test
    difficulte: str = ""  # facile | moyen | difficile | temoin
    origine: str = ""  # amorcage | genere | controle_negatif
    classe: str = ""  # QID de la classe (sujets générés)
    r: int = 0
    part_site: float = 0.0
    part_siren: float = 0.0
    notes: str = ""
    where: str = ""
    classes_ok: list[str] = field(default_factory=list)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8"
        )


def classes_organisations(w: Wikidata, log=print) -> dict[str, dict]:
    """Classes d'organisations (P279* organisation) de 20 à 250 instances françaises avec site officiel."""
    rows = w.sparql(CLASSES_QUERY)
    ids = [r["class"] for r in rows]
    n = {r["class"]: int(r["n"]) for r in rows}
    labels: dict[str, str] = {}
    for i in range(0, len(ids), 50):
        d = w.api(
            action="wbgetentities", ids="|".join(ids[i : i + 50]), props="labels", languages="fr|en"
        )
        for k, v in d["entities"].items():
            lab = v.get("labels", {})
            labels[k] = (lab.get("fr") or lab.get("en") or {}).get("value", "")
    org: set[str] = set()
    for i in range(0, len(ids), 10):
        chunk = ids[i : i + 10]
        q = "SELECT DISTINCT ?class WHERE { VALUES ?class { %s } ?class wdt:P279* wd:%s . }" % (
            " ".join("wd:" + c for c in chunk), ORG,
        )  # fmt: skip
        try:
            org |= {r["class"] for r in w.sparql(q)}
        except WikidataError as exc:
            log(f"classes {i}-{i + 10} : {exc}")
    return {c: {"label": labels.get(c, ""), "n": n[c]} for c in sorted(org) if labels.get(c)}


def mesurer_classe(w: Wikidata, qid: str) -> dict:
    """Taille de la référence (instances directes en France) et parts avec site officiel / SIREN."""
    q = (
        "SELECT (COUNT(DISTINCT ?item) AS ?n) (COUNT(DISTINCT ?s) AS ?ns) (COUNT(DISTINCT ?r) AS ?nr) WHERE { "
        f"?item wdt:P31 wd:{qid} ; wdt:P17 wd:{FR} . OPTIONAL {{ ?item wdt:P856 ?s }} OPTIONAL {{ ?item wdt:P1616 ?r }} }}"
    )
    row = w.sparql(q)[0]
    n = int(row["n"])
    return {
        "n": n,
        "part_site": int(row["ns"]) / n if n else 0.0,
        "part_siren": int(row["nr"]) / n if n else 0.0,
    }


def phrase_de(label: str) -> str:
    return f"{label[:1].upper()}{label[1:]} en France"


def tirer(
    w: Wikidata, classes: dict[str, dict], combien: int, seed: int = SEED, log=print
) -> list[Sujet]:
    """Tire au hasard (graine fixée) `combien` classes dont la référence compte 20 à 200 entités."""
    rng = random.Random(seed)
    ordre = sorted(classes)
    rng.shuffle(ordre)
    out: list[Sujet] = []
    for qid in ordre:
        if len(out) >= combien:
            break
        try:
            m = mesurer_classe(w, qid)
        except WikidataError as exc:
            log(f"{qid} ignorée : {exc}")
            continue
        if not 20 <= m["n"] <= 200:
            continue
        lab = classes[qid]["label"]
        if any(x.notes == lab for x in out):
            continue  # deux classes de même libellé : sujets non disjoints
        out.append(
            Sujet(
                id=f"g-{qid.lower()}",
                phrase=phrase_de(lab),
                origine="genere",
                classe=qid,
                r=m["n"],
                part_site=round(m["part_site"], 3),
                part_siren=round(m["part_siren"], 3),
                where=class_where(qid),
                classes_ok=[qid],
                notes=lab,
            )  # fmt: skip
        )
    return out


def difficultes(sujets: list[Sujet]) -> None:
    """Difficulté par terciles de la présence (moyenne part_site, part_siren) : haute = facile."""
    ordre = sorted(sujets, key=lambda s: (-(s.part_site + s.part_siren) / 2, s.id))
    n = len(ordre)
    for i, s in enumerate(ordre):
        s.difficulte = "facile" if i < n / 3 else "moyen" if i < 2 * n / 3 else "difficile"


def construire_reference(w: Wikidata, s: Sujet) -> Reference:
    return build(w, s.id, s.phrase, s.where, s.classes_ok)


# --- sujets d'amorçage fournis par Sednal (DECISION_AUTOCAL.md) -------------------------------------------------
# Référence = requête Wikidata structurée. Les sujets dont la référence est trop pauvre pour une R-précision
# (R < 5 : aide alimentaire en Auvergne-Rhône-Alpes, collectifs de locataires en Île-de-France) ne sont pas collectés :
# aucune référence Wikidata, donc rien à mesurer (consigné dans calibration.md, jamais présenté comme réussi).
AMORCAGE: list[Sujet] = [
    Sujet(
        id="handball-pro",
        phrase="Les clubs de handball professionnels en France",
        origine="amorcage",
        difficulte="moyen",
        where="?item wdt:P118 ?l . VALUES ?l { wd:Q203172 wd:Q2951241 wd:Q522560 wd:Q2951213 } FILTER NOT EXISTS { ?item wdt:P31 wd:Q5 } ",
        classes_ok=["Q108320110", "Q10517054", "Q847017"],
        notes="clubs de première et deuxième division masculine, tels que listés dans Wikidata (P118)",
    ),
    Sujet(
        id="cac40",
        phrase="Les entreprises du CAC 40",
        origine="amorcage",
        difficulte="facile",
        where="?item p:P361 ?s . ?s ps:P361 wd:Q648828 . FILTER NOT EXISTS { ?s pq:P582 ?e } ",
        classes_ok=["Q4830453", "Q6881511", "Q783794"],
        notes="composition actuelle de l'indice (P361 sans date de fin)",
    ),
    Sujet(
        id="supporters-foot",
        phrase="Les groupes de supporters de clubs de football français",
        origine="amorcage",
        difficulte="difficile",
        where="?item wdt:P31/wdt:P279* wd:Q1070414 . ?item wdt:P17 wd:Q142 . ",
        classes_ok=["Q1070414", "Q386636"],
        notes="référence très petite : R-précision bruitée",
    ),
    Sujet(
        id="festivals-elec",
        phrase="Les festivals de musique électronique en France",
        origine="amorcage",
        difficulte="difficile",
        where="?item wdt:P31/wdt:P279* wd:Q868557 ; wdt:P17 wd:Q142 ; wdt:P136 ?g . ?g wdt:P279* wd:Q9778 . ",
        classes_ok=["Q868557", "Q131321883", "Q15275719"],
        notes="référence très petite : R-précision bruitée",
    ),
]
AMORCAGE_NON_MESURABLES = {
    "aide-alimentaire-ara": "Les associations d'aide alimentaire en Auvergne-Rhône-Alpes",
    "locataires-idf": "Les collectifs de défense des locataires en Île-de-France",
}
TEMOINS: list[
    Sujet
] = [  # contrôles négatifs : aucun acteur réel (le premier est fourni par Sednal)
    Sujet(
        id="temoin-licornes",
        phrase="Les fabricants de licornes bleues en Lozère",
        origine="controle_negatif",
        difficulte="temoin",
    ),
    Sujet(
        id="temoin-dragons",
        phrase="Les éleveurs de dragons verts en Corrèze",
        origine="controle_negatif",
        difficulte="temoin",
    ),
    Sujet(
        id="temoin-sorciers",
        phrase="Les écoles de sorcellerie de Haute-Savoie",
        origine="controle_negatif",
        difficulte="temoin",
    ),
]


# --- composition des trois jeux ------------------------------------------------------------------------------------
JEUX = ("dev", "validation", "test")
PAR_STRATE = 2  # sujets positifs par difficulté et par jeu (+ un contrôle négatif par jeu)
PREFERENCE = {
    "handball-pro": "dev",
    "festivals-elec": "dev",
    "cac40": "validation",
    "supporters-foot": "validation",
}


def composer(w: Wikidata, generes: list[Sujet], seed: int = SEED, log=print) -> list[Sujet]:
    """Trois jeux disjoints, même proportion de difficultés (2 faciles, 2 moyens, 2 difficiles + 1 témoin).

    La difficulté des sujets d'amorçage est calculée comme celle des sujets générés (mêmes seuils de terciles) :
    présence de leurs acteurs dans Wikidata (site officiel) et dans les registres (SIREN)."""
    ordre = sorted(generes, key=lambda s: (-(s.part_site + s.part_siren) / 2, s.id))
    n = len(ordre)
    seuils = (
        (ordre[n // 3 - 1].part_site + ordre[n // 3 - 1].part_siren) / 2,
        (ordre[2 * n // 3 - 1].part_site + ordre[2 * n // 3 - 1].part_siren) / 2,
    )
    difficultes(generes)
    amorces = []
    for s in AMORCAGE:
        ref = build(w, s.id, s.phrase, s.where, s.classes_ok)
        s.r = ref.r
        s.part_site = round(sum(1 for e in ref.entities if e.domains) / ref.r, 3) if ref.r else 0.0
        s.part_siren = round(sum(1 for e in ref.entities if e.siren) / ref.r, 3) if ref.r else 0.0
        score = (s.part_site + s.part_siren) / 2
        s.difficulte = (
            "facile" if score >= seuils[0] else "moyen" if score >= seuils[1] else "difficile"
        )
        amorces.append(s)
        log(f"{s.id} : R={s.r} site={s.part_site} siren={s.part_siren} -> {s.difficulte}")
    rng = random.Random(seed)
    place: dict[tuple[str, str], list[Sujet]] = {
        (j, d): [] for j in JEUX for d in ("facile", "moyen", "difficile")
    }
    for s in amorces:
        pref = PREFERENCE.get(s.id)
        cands = ([pref] if pref else []) + [j for j in JEUX if j != pref]
        for j in cands:
            if len(place[j, s.difficulte]) < PAR_STRATE:
                s.jeu = j
                place[j, s.difficulte].append(s)
                break
    pool = {d: [s for s in generes if s.difficulte == d] for d in ("facile", "moyen", "difficile")}
    for d in pool:
        rng.shuffle(pool[d])
    for (j, d), lst in place.items():
        while len(lst) < PAR_STRATE and pool[d]:
            s = pool[d].pop()
            s.jeu = j
            lst.append(s)
    temoins = list(TEMOINS)
    for j, t in zip(JEUX, temoins, strict=True):
        t.jeu = j
    return [s for lst in place.values() for s in lst] + temoins
