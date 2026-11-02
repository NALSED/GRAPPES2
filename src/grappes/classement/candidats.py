"""Tableau des candidats d'un sujet, calculé depuis la base d'un rejeu (aucun réseau).

Un candidat = une entité (clé de source) avec le score ACTUEL du système (`confidence` de `assign`), ses
composantes (S sémantique, E recoupement, A ancrage) et les attributs utiles aux autres scores : nombre de citants,
écosystèmes, fiabilité. `exportable` : l'entité a un nom lisible (sinon `resultat.csv` ne peut pas la rendre).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from grappes.canon import canonical_domain, site_key
from grappes.clustering.assign import AMBIGU, CLASSE, assign
from grappes.clustering.resultat import readable_name
from grappes.config import Config, load_config
from grappes.fiabilite import Reliability
from grappes.pipeline import reliability_for
from grappes.storage.db import Database


@dataclass(slots=True)
class Candidat:
    ref: str
    nom: str | None  # nom lisible, ou None (domaine seul, identifiant technique)
    nom_brut: str  # nom affiché connu, ou identifiant
    confiance: float  # score actuel du système (0 à 100)
    statut: str
    s: float
    e: float
    a: float
    citants: set[str] = field(default_factory=set)  # sources citantes (clés)
    ecosystemes: set[str] = field(default_factory=set)
    editeurs: int = 0  # nombre d'éditeurs distincts parmi les citants
    lettre: str = "F"
    facteur: float = 1.0
    propre: bool = False  # l'entité a un texte propre (source lue)
    registres: set[str] = field(default_factory=set)  # registres qui la nomment (wikidata, sirene)
    ville: str = ""  # commune donnée par un registre, jamais déduite
    ner_sites: dict[str, float] = field(default_factory=dict)  # éditeur -> confiance moyenne du modèle (noms dans les textes)

    @property
    def exportable_actuel(self) -> bool:
        return self.statut in (CLASSE, AMBIGU) and self.nom is not None


@dataclass(slots=True)
class Contexte:
    cfg: Config
    db: Database
    rel: Reliability
    grappe: str
    candidats: list[Candidat]
    citations: list[
        tuple[str, str]
    ]  # (citant, cité), liens explicites et mentions, sans hypothèses
    lues: set[str]  # sources dont on a lu le texte
    nommes: dict = field(default_factory=dict)  # organisations nommées dans les textes (chantier 2)
    decouverts: list = field(default_factory=list)  # entités de registre (chantier 1), archivées par sujet
    memo_variantes: dict = field(default_factory=dict)  # regroupements nomenklatura déjà calculés


def charger(cible: str | Path | None, cfg: Config | None = None, db: Database | None = None) -> Contexte:
    """Contexte d'un sujet depuis un dossier de rejeu (`cible`) ou, en production, depuis la base et la configuration
    déjà ouvertes (`db` et `cfg`)."""
    if db is None:
        cible = Path(cible)  # type: ignore[arg-type]
        cfg = cfg or load_config(cible / "config")
        db = Database(cible / "grappes.db")
    assert cfg is not None
    rel = reliability_for(db, cfg)
    obs = db.load_observations()
    result = assign(obs, cfg, names=db.load_display_names(), reliability=rel)
    gid = cfg.grappes[0].grappe_id
    ents = {e.entity_id: e for e in result.entities}
    cits = [(c.citer, c.cited) for c in result.citations if not c.hypothesis]
    editeurs: dict[str, set[str]] = {}
    for citer, cited in cits:
        eco, _, ident = citer.partition(":")
        editeurs.setdefault(cited, set()).add(site_key(ident) if eco in ("web", "rss") else citer)
    lues = {o.source.key for o in obs}
    out: list[Candidat] = []
    for m in result.memberships:
        if m["grappe_id"] != gid:
            continue
        e = ents[m["entity_id"]]
        ref = e.primary_ref
        brut = result.names.get(ref) or ref.split(":", 1)[-1]
        st = result.stats[m["entity_id"]]
        out.append(
            Candidat(
                ref, readable_name(result.names.get(ref), ref.split(":", 1)[-1]), brut,
                m["confidence"], m["status"], m["semantic"], m["ecosystem"], m["anchor"],
                set(st.citing), set(st.ecosystems), len(editeurs.get(ref, ())),
                rel.letter(ref), rel.factor(ref), ref in lues,
            )
        )  # fmt: skip
    return Contexte(cfg, db, rel, gid, sorted(out, key=lambda c: c.ref), sorted(set(cits)), lues)


def domaine(ref: str) -> str:
    eco, _, ident = ref.partition(":")
    return canonical_domain(ident) if eco in ("web", "rss") else ""
