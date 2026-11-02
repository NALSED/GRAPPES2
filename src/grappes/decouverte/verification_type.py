"""Vérificateur de type (D69, point 2) : avant de classer un candidat, vérifier que son type correspond au type demandé.

C'est le mécanisme qui a fait gagner Purdue au TREC Entity 2010 (appariement entre les types de l'entité cible et ceux
du candidat). Ici il est appliqué EN AMONT, à la découverte : on ne filtre pas après coup d'après un score.

Le type demandé vient de la seule phrase du sujet : son premier mot plein (« clubs », « festivals », « chorale »...),
SANS le complément de domaine (« handball ») : un nom qui contient « handball » ne prouve pas que ce soit un club. La nature demandée est toujours « organisation »
(grappes ne collecte pas de personnes tant que `config/personnes.yaml` a `collecte: false`).

Le type du candidat est ce que SA SOURCE déclare : `type_fixe` du jeu de données (« festival »), activité de la ligne
(code NAF, remplacé par son libellé), nature juridique. Verdict à trois valeurs :
* `compatible` : le type déclaré porte un mot du type demandé ;
* `incompatible` : la source déclare une nature « personne physique », ou un type qui ne partage aucun mot avec le type
  demandé. Le NOM du candidat n'est pas une preuve de type : il a servi à le trouver (la requête, relaxée au type seul,
  cherche précisément ce mot dans le nom), donc la preuve serait circulaire ;
* `inconnu` : la source ne dit rien du type (data.bnf.fr : « Organization » seulement). Un inconnu est GARDÉ : on
  n'écarte que sur une preuve, on ne devine pas le type d'après le nom.

Limite connue : la comparaison est lexicale (racines de mots). Un type équivalent écrit autrement (« compagnie » pour
« troupe », « arts du spectacle vivant » pour « troupe théâtrale ») est jugé incompatible : le rapport chiffre ces
écartés. Aucune liste de synonymes n'est inventée.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from grappes.decouverte.registres import Decouvert
from grappes.decouverte.traduction import ARTICLES, mots, noyau, racine

COMPATIBLE, INCOMPATIBLE, INCONNU = "compatible", "incompatible", "inconnu"
_CODE_NAF = re.compile(r"^\s*(\d{2})\.?(\d{2}[A-Za-z])\b")
_PERSONNE_PHYSIQUE = re.compile(r"personne\s+physique", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TypeDemande:
    nature: str  # "organisation"
    vocabulaire: frozenset[str]  # racines des mots du type demandé


def _racines(texte: str) -> set[str]:
    return {racine(m) for m in mots(texte) if m not in ARTICLES and len(m) > 1}


def demande_type(phrase: str) -> TypeDemande:
    """Type demandé, de la seule phrase : la racine de son premier mot plein."""
    n = noyau(phrase)
    return TypeDemande("organisation", frozenset({racine(n[0])} if n else ()))


def _libelle_type(texte: str, naf: dict[str, str]) -> str:
    """Un code NAF (« 90.01Z », « 9001Z - Arts du spectacle vivant ») est remplacé par son libellé officiel."""
    m = _CODE_NAF.match(texte)
    if not m:
        return texte
    return naf.get(f"{m.group(1)}.{m.group(2).upper()}", texte)


def verifier(d: Decouvert, dem: TypeDemande, naf: dict[str, str]) -> tuple[str, str]:
    """(verdict, motif) pour un candidat de prise."""
    if d.nature and _PERSONNE_PHYSIQUE.search(d.nature):
        return INCOMPATIBLE, f"nature « {d.nature} » : la cible est une organisation"
    if not d.type_candidat or not dem.vocabulaire:
        return INCONNU, "la source ne déclare pas de type"
    libelle = _libelle_type(d.type_candidat, naf)
    if dem.vocabulaire & _racines(libelle):
        return COMPATIBLE, f"type « {libelle} »"
    return INCOMPATIBLE, f"type « {libelle} » sans mot de « {' '.join(sorted(dem.vocabulaire))} »"


def filtrer(ds: list[Decouvert], dem: TypeDemande, naf: list[tuple[str, str]] | dict[str, str]) -> tuple[list[Decouvert], list[tuple[Decouvert, str]]]:
    """(gardés, écartés avec leur motif). Les compatibles et les inconnus sont gardés."""
    table = dict(naf)
    gardes: list[Decouvert] = []
    ecartes: list[tuple[Decouvert, str]] = []
    for d in ds:
        v, motif = verifier(d, dem, table)
        if v == INCOMPATIBLE:
            ecartes.append((d, motif))
        else:
            gardes.append(d)
    return gardes, ecartes
