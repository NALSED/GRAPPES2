"""Personne physique ou compte institutionnel : seulement par des indices explicites.

Un compte (X, Instagram) est une personne par défaut. Il devient « institutionnel » (type `group`)
seulement si son nom lisible contient un indice explicite (association, journal, radio, mairie...).
En cas de doute, ou sans nom, on le traite comme une personne (FINAL.md § 9).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from grappes.semantic.scoring import normalize

DEFAULT_INSTITUTION_HINTS = (
    "association", "assoc", "ministère", "mairie", "ville de", "préfecture", "gouvernement",
    "journal", "quotidien", "gazette", "revue", "magazine", "média", "medias", "presse", "news",
    "radio", "tv", "télévision", "chaîne", "agence", "éditions", "université", "école", "lycée",
    "institut", "fondation", "fédération", "syndicat", "collectif", "coopérative", "ong",
    "société", "entreprise", "compagnie", "musée", "bibliothèque", "hôpital", "club",
    "official", "officiel", "officielle", "ministry", "newspaper", "university", "foundation",
    "association", "agency", "council", "city of", "department", "museum", "library", "company",
)  # fmt: skip


def _words(text: str) -> list[str]:
    return re.sub(r"[^\w]+", " ", normalize(text)).split()


def is_institution(name: str | None, hints: tuple[str, ...] = DEFAULT_INSTITUTION_HINTS) -> bool:
    """Vrai si `name` contient un indice institutionnel (mot entier ou suite de mots entiers)."""
    if not name:
        return False
    words = _words(name)
    padded = f" {' '.join(words)} "
    return any(f" {' '.join(_words(h))} " in padded for h in hints if _words(h))


@dataclass(frozen=True, slots=True)
class PerimetrePersonnes:
    """Périmètre des personnes (D64, étape 4) : préparé en configuration, jamais activé par défaut."""

    collecte: bool = False
    perimetre: str = "roles_publics"
    roles_publics: tuple[str, ...] = ("dirigeant_registre", "porte_parole_cite", "compte_public_structure")
    masquage: bool = True


def load_perimetre(path: str | Path) -> PerimetrePersonnes:
    """`config/personnes.yaml`. Absent : aucune collecte, masquage actif. Valeur inconnue : erreur, jamais de repli permissif."""
    from grappes.config import ConfigError  # import tardif : config importe déjà ce module

    f = Path(path)
    if not f.exists():
        return PerimetrePersonnes()
    y = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    base = PerimetrePersonnes()
    perimetre = str(y.get("perimetre", base.perimetre))
    if perimetre not in ("roles_publics", "aucune"):
        raise ConfigError(f"{f}: perimetre doit valoir roles_publics ou aucune (les particuliers ne sont jamais dans le périmètre)")
    roles = tuple(y.get("roles_publics", base.roles_publics))
    inconnus = [r for r in roles if r not in base.roles_publics]
    if inconnus:
        raise ConfigError(f"{f}: rôles publics inconnus {inconnus}")
    return PerimetrePersonnes(bool(y.get("collecte", False)), perimetre, roles, bool(y.get("masquage", True)))
