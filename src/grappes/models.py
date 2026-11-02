"""Modèle de données standard (dataclasses immuables)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class EntityType(str, Enum):
    PERSON = "person"
    GROUP = "group"
    SITE = "site"


@dataclass(frozen=True, slots=True)
class SourceRef:
    ecosystem: str  # "x" | "telegram" | "web" | ...
    ident: str  # handle / canal / domaine, canonicalisé

    @property
    def key(self) -> str:
        return f"{self.ecosystem}:{self.ident}"


@dataclass(frozen=True, slots=True)
class Observation:
    obs_id: str  # hash stable (écosystème + id natif + texte)
    source: SourceRef
    ts: datetime | None
    text: str
    mentions: tuple[SourceRef, ...] = ()
    links: tuple[str, ...] = ()  # URLs canonicalisées
    hashtags: tuple[str, ...] = ()
    engagement: int = 0
    raw_ref: str | None = None  # pointeur vers le JSON d'origine
    # clés (source ou éléments cités) dont l'identité n'est qu'une hypothèse (D25) : une citation
    # qui les implique est conservée et signalée, mais ne valide jamais un recoupement
    hypothetical: tuple[str, ...] = ()
    meta: dict[str, str] = field(default_factory=dict, compare=False)


@dataclass(frozen=True, slots=True)
class Entity:
    entity_id: str
    type: EntityType
    primary_ref: str  # SourceRef.key ou url:domaine
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class Lexicon:
    terms: dict[str, float]  # terme -> poids (> 0)
    negative: dict[str, float] = field(default_factory=dict)  # terme -> pénalité (> 0)


@dataclass(frozen=True, slots=True)
class Grappe:
    grappe_id: str
    label: str
    lexicon: Lexicon
    anchors: tuple[str, ...] = ()  # clés canoniques
    oppositions: tuple[str, ...] = ()  # grappe_id ou clés canoniques
    scale: float = 3.0  # paramètre de saturation du scoring
    learned_terms: tuple[str, ...] = ()  # termes du « lexique_appris » fusionnés dans le lexique
    subject: str = ""  # phrase du sujet quand la grappe en vient (lexique amorcé automatiquement)
