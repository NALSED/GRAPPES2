"""Traduire la phrase d'un sujet en requêtes de registre (cœur du chantier 1, D63).

Aucune valeur n'est lue dans la référence d'évaluation : la traduction part de la seule phrase.

* Noyau : la phrase sans article, sans complément de lieu (« en France », « de Haute-Savoie »…), en mots normalisés.
* Wikidata : on cherche (API `wbsearchentities`) les segments du noyau, du plus long au plus court ; un résultat n'est
  retenu que si son libellé (ou un alias affiché) est identique au segment (pluriel ignoré) ET s'il a au moins
  `min_instances` instances dans le pays. Aucun résultat plausible : on ne rend rien (on n'invente pas de classe).
* SIRENE/RNA : le code NAF dont le libellé recouvre le mieux le noyau (recouvrement >= `seuil_naf`), sinon une
  recherche par nom limitée aux résultats dont le nom contient le segment retenu.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from grappes.decouverte.client import DecouverteError, Http
from grappes.semantic.scoring import normalize

ARTICLES = {"le", "la", "les", "l", "un", "une", "des", "du", "de", "d", "au", "aux", "et", "en", "a", "dans", "sur", "pour"}
LIEUX = re.compile(
    r"\s+(?:en|de|du|dans(?:\s+le|\s+la|\s+les)?|d')\s*[A-ZÉÈÀ][\w\-']*(?:[\s-][A-ZÉÈÀ][\w\-']*)*$|\s+fran[cç]ais(?:es?)?\b.*$"
)


def mots(texte: str) -> list[str]:
    return [m for m in re.split(r"[^\w]+", normalize(texte)) if m]


def racine(mot: str) -> str:
    """Pluriel et féminin les plus courants ignorés (s, x, e finaux sur les mots de plus de 3 lettres)."""
    if len(mot) > 3 and mot[-1] in "sx":
        mot = mot[:-1]
    if len(mot) > 4 and mot.endswith("e"):
        mot = mot[:-1]
    return mot


def singulier(mot: str) -> str:
    """Pluriel en -s ou -x retiré seulement (la recherche Wikidata veut des mots entiers : « groupe », pas « group »)."""
    return mot[:-1] if len(mot) > 3 and mot[-1] in "sx" else mot


def noyau(phrase: str) -> list[str]:
    """Mots porteurs de sens de la phrase (sans complément de lieu ni articles), dans l'ordre."""
    p = LIEUX.sub("", phrase.strip())
    p = re.sub(r"^(?:les?|la|l'|des?|du|un|une)\s+", "", p, flags=re.IGNORECASE)
    return [m for m in mots(p) if m not in ARTICLES]


def segments(mots_noyau: list[str], tous: list[str]) -> list[list[str]]:
    """Sous-suites contiguës du noyau écrit avec ses petits mots, du plus long au plus court."""
    n = len(tous)
    out = []
    for taille in range(n, 0, -1):  # le nom noyau est en tête en français : segments commençant au premier mot
        seg = tous[:taille]
        if seg[-1] not in ARTICLES:
            out.append(seg)
    return out


def _mots_phrase_sans_lieu(phrase: str) -> list[str]:
    p = LIEUX.sub("", phrase.strip())
    p = re.sub(r"^(?:les?|la|l'|des?|du|un|une)\s+", "", p, flags=re.IGNORECASE)
    return mots(p)


@dataclass(slots=True)
class ClasseWikidata:
    qid: str
    libelle: str
    segment: str
    instances: int
    complet: bool = True  # le segment couvre tout le noyau de la phrase (sinon : classe partielle)


def classe_wikidata(
    phrase: str, http: Http, pays: str = "Q142", min_instances: int = 3, plafond: int = 1001
) -> ClasseWikidata | None:
    """Classe Wikidata la plus plausible pour la phrase, ou None."""
    tous = _mots_phrase_sans_lieu(phrase)
    plein = len(noyau(phrase))
    for seg in segments(noyau(phrase), tous):
        if plein > 1 and len([m for m in seg if m not in ARTICLES]) < 2:
            continue  # un seul mot d'une expression composée : trop ambigu pour désigner une classe
        texte = " ".join(seg)
        cible = " ".join(racine(m) for m in seg)
        hits: list[dict] = []
        for variante in dict.fromkeys([texte, " ".join(singulier(m) for m in seg)]):
            try:
                hits += http.wd_search(variante, 8)  # l'API ne trouve pas toujours le pluriel
            except DecouverteError:
                continue
        for h in hits:
            lib = h.get("label", "") or ""
            if " ".join(racine(m) for m in mots(lib)) != cible:
                continue
            q = (
                "SELECT (COUNT(*) AS ?n) WHERE { { SELECT ?i WHERE { ?i wdt:P31 wd:%s ; wdt:P17 wd:%s . } LIMIT %d } }"
                % (h["id"], pays, plafond)
            )
            try:
                n = int(http.sparql(q)[0]["n"])
            except (DecouverteError, KeyError, IndexError, ValueError):
                continue
            if min_instances <= n < plafond:  # n >= plafond : classe trop générale (tout le pays), pas un sujet
                return ClasseWikidata(h["id"], lib, texte, n, len([m for m in seg if m not in ARTICLES]) == plein)
    return None


@dataclass(slots=True)
class Lecture:
    qid: str
    libelle: str
    description: str
    instances: int  # éléments de cette classe dans le pays (information, jamais un critère de choix)


def lectures(phrase: str, http: Http, pays: str = "Q142") -> list[Lecture]:
    """Toutes les lectures possibles du sujet (D68, chantier B) : éléments Wikidata dont le libellé est identique au
    segment retenu de la phrase (« brasserie » : lieu de brassage, café-restaurant...). Plus d'une lecture = sujet
    ambigu. Cette fonction LISTE ; elle ne départage pas et n'invente aucune règle pour le faire."""
    tous = _mots_phrase_sans_lieu(phrase)
    plein = len(noyau(phrase))
    for seg in segments(noyau(phrase), tous):
        if plein > 1 and len([m for m in seg if m not in ARTICLES]) < 2:
            continue
        cible = " ".join(racine(m) for m in seg)
        trouves: dict[str, Lecture] = {}
        for variante in dict.fromkeys([" ".join(seg), " ".join(singulier(m) for m in seg)]):
            try:
                hits = http.wd_search(variante, 10)
            except DecouverteError:
                continue
            for h in hits:
                if " ".join(racine(m) for m in mots(h.get("label", "") or "")) == cible and h["id"] not in trouves:
                    trouves[h["id"]] = Lecture(h["id"], h.get("label", ""), h.get("description", "") or "", -1)
        if trouves:
            for lec in trouves.values():
                q = "SELECT (COUNT(*) AS ?n) WHERE { { SELECT ?i WHERE { ?i wdt:P31 wd:%s ; wdt:P17 wd:%s . } LIMIT 1001 } }" % (lec.qid, pays)
                try:
                    lec.instances = int(http.sparql(q)[0]["n"])
                except (DecouverteError, KeyError, IndexError, ValueError):
                    pass
            return sorted(trouves.values(), key=lambda x: x.qid)
    return []


@dataclass(slots=True)
class CodeNaf:
    code: str
    libelle: str
    recouvrement: float
    reste: list[str]  # mots du noyau absents du libellé NAF (ils précisent la recherche par nom)


def charger_naf(path: str | Path) -> list[tuple[str, str]]:
    with open(path, encoding="utf-8", newline="") as f:
        r = csv.reader(f, delimiter=";")
        next(r)
        return [(c, lib) for c, lib in r]


def code_naf(phrase: str, naf: list[tuple[str, str]], seuil: float = 0.6) -> CodeNaf | None:
    """NAF dont le libellé recouvre le mieux le noyau de la phrase. Égalité de recouvrement : libellé le plus court."""
    nuc = [racine(m) for m in noyau(phrase)]
    if not nuc:
        return None
    meilleur: tuple[float, int, str, str] | None = None
    for code, lib in naf:
        lm = {racine(m) for m in mots(lib) if m not in ARTICLES}
        rec = sum(1 for m in nuc if m in lm) / len(nuc)
        if rec >= seuil and (meilleur is None or (rec, -len(lm)) > (meilleur[0], -meilleur[1])):
            meilleur = (rec, len(lm), code, lib)
    if meilleur is None:
        return None
    lm = {racine(m) for m in mots(meilleur[3])}
    return CodeNaf(meilleur[2], meilleur[3], meilleur[0], [m for m in noyau(phrase) if racine(m) not in lm])


# --- traduction « domaine » : type + complément (« clubs de HANDBALL », « festivals de MUSIQUE ÉLECTRONIQUE ») ----------
LIENS_DOMAINE = ("P641", "P136", "P101", "P921", "P361", "P463", "P452")
# sport, genre, domaine d'activité, sujet principal, partie de, membre de, secteur : propriétés Wikidata qui rattachent
# une organisation à un domaine. Liste fixe et documentée (config/decouverte.yaml), jamais choisie sujet par sujet.


@dataclass(slots=True)
class DomaineWikidata:
    entite: str  # QID du complément (ex. Q8418 handball)
    libelle: str
    segment: str  # mots du sujet qui l'ont désigné
    type_qid: str  # classe du type (ex. festival), ou "" si introuvable
    ignores: list[str]  # mots du sujet non résolus (adjectifs : « professionnels »), signalés, jamais devinés
    pays: str  # QID du pays si la phrase en donne un (« en France »), sinon ""


def pays_de_la_phrase(phrase: str) -> str:
    return "Q142" if re.search(r"\bFrance\b|\bfran[cç]ais(?:es?)?\b", phrase) else ""


def _entite_exacte(texte: str, http: Http) -> tuple[str, str] | None:
    cible = " ".join(racine(m) for m in mots(texte))
    variantes = dict.fromkeys([texte, " ".join(singulier(m) for m in mots(texte))])
    for v in variantes:
        try:
            hits = http.wd_search(v, 8)
        except DecouverteError:
            continue
        for h in hits:  # la recherche classe par pertinence : on garde le premier libellé identique
            if " ".join(racine(m) for m in mots(h.get("label", "") or "")) == cible:
                return h["id"], h.get("label", "")
    return None


def domaine_wikidata(phrase: str, http: Http) -> DomaineWikidata | None:
    """Sépare la phrase en TYPE (premier mot du noyau) + COMPLÉMENT (le reste). Le complément est cherché dans
    Wikidata ; les derniers mots sont retirés un à un tant qu'il est introuvable (au plus deux), et signalés."""
    tous = _mots_phrase_sans_lieu(phrase)
    plein = noyau(phrase)
    if len(plein) < 2:
        return None
    # le complément commence après le premier mot plein ; les petits mots de liaison ("de") sont écartés
    debut = next(i for i, m in enumerate(tous) if m == plein[0]) + 1
    compl = [m for m in tous[debut:]]
    while compl and compl[0] in ARTICLES:
        compl = compl[1:]
    ignores: list[str] = []
    for _ in range(3):
        if not compl:
            return None
        texte = " ".join(compl)
        e = _entite_exacte(texte, http)
        if e:
            t = _entite_exacte(plein[0], http)
            return DomaineWikidata(e[0], e[1], texte, t[0] if t else "", ignores, pays_de_la_phrase(phrase))
        ignores.insert(0, compl[-1])
        compl = compl[:-1]
    return None
