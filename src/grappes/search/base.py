"""Interface `SearchProvider`, registre par type, contexte d'exécution et journal des appels."""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from grappes import avancement
from grappes.search.models import Candidate


class SearchError(Exception):
    """Échec d'une recherche (réponse mal formée, erreur HTTP...)."""


class ProviderUnavailable(SearchError):
    """Fournisseur inactif : clé absente, configuration incomplète. Aucun appel n'est fait."""


class ProviderStop(SearchError):
    """Le fournisseur refuse (403, 429) : on arrête de l'interroger pour ce cycle."""


class QuotaExceeded(SearchError):
    """Quota de requêtes atteint."""


class Journal:
    """Journal des appels : fournisseur, date, requête, statut. Jamais de secret."""

    def __init__(self, path: Path | None):
        self.path = path
        self.secrets: set[str] = set()

    def add_secret(self, value: str | None) -> None:
        if value and len(value) >= 4:
            self.secrets.add(value)

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def log(self, provider: str, keyword: str, request: str, status: str) -> None:
        if request not in ("", "-"):  # une vraie requête (les refus et inactivités n'en sont pas) : compte exact d'appels
            avancement.signaler("appel", prise=provider, statut=status)
        if self.path is None:
            return
        line = " | ".join(
            self.redact(x.replace("\n", " ").replace("|", "/"))
            for x in (
                datetime.now(UTC).isoformat(timespec="seconds"),
                provider,
                keyword,
                request,
                status,
            )
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


@dataclass(slots=True)
class SearchContext:
    """Ce que le moteur donne à un fournisseur : exécution réelle ou non, quotas, délai, journal."""

    execute: bool = False  # faux = dry-run : aucun appel externe (vrai seulement si `autonome`)
    sujet: str = ""  # phrase du sujet en cours, pour le gabarit {sujet}
    journal: Journal = field(default_factory=lambda: Journal(None))
    delay_s: float = 1.0
    quota_total: int = 100
    sleep: Callable[[float], None] = time.sleep
    used_total: int = 0
    used: dict[str, int] = field(default_factory=dict)
    _last_call: float = 0.0

    def take(self, provider: str, quota: int | None) -> None:
        """Compte une requête ; `QuotaExceeded` si le quota du fournisseur ou le total est atteint."""
        if self.used_total >= self.quota_total:
            raise QuotaExceeded(f"quota total atteint ({self.quota_total})")
        if quota is not None and self.used.get(provider, 0) >= quota:
            raise QuotaExceeded(f"quota de {provider} atteint ({quota})")
        self.used_total += 1
        self.used[provider] = self.used.get(provider, 0) + 1

    def throttle(self) -> None:
        """Délai minimal entre deux requêtes (tous fournisseurs confondus)."""
        wait = self._last_call + self.delay_s - time.monotonic()
        if wait > 0 and self._last_call:
            self.sleep(wait)
        self._last_call = time.monotonic()

    def env_secret(self, name: str | None) -> str:
        """Valeur d'une variable d'environnement (les secrets n'ont pas d'autre source)."""
        if not name:
            raise ProviderUnavailable("variable d'environnement non précisée")
        value = os.environ.get(name, "")
        if not value:
            raise ProviderUnavailable(f"clé absente : variable d'environnement {name}")
        self.journal.add_secret(value)
        return value


class SearchProvider(ABC):
    """Un fournisseur de recherche. Entrée : un mot-clé ; sortie : des candidats nettoyés.

    Un fournisseur est configuré par un fichier YAML (types génériques) ou écrit comme plugin
    Python dans `plugins/`. Il ne touche jamais au cœur : il n'a accès qu'à son `ctx`.
    """

    type: ClassVar[str]

    def __init__(self, provider_id: str, options: Mapping[str, Any], ctx: SearchContext):
        self.id = provider_id
        self.options = dict(options)
        self.ctx = ctx

    @abstractmethod
    def search(self, keyword: str, limit: int) -> Iterable[Candidate]:
        """Résultats pour un mot-clé. En dry-run (`ctx.execute` faux) : journaliser sans appeler."""


_REGISTRY: dict[str, type[SearchProvider]] = {}


def register_provider(cls: type[SearchProvider]) -> type[SearchProvider]:
    """Décorateur : enregistre un type de fournisseur (`cls.type`)."""
    kind = getattr(cls, "type", None)
    if not kind:
        raise ValueError(f"{cls.__name__} n'a pas d'attribut `type`")
    if kind in _REGISTRY and _REGISTRY[kind] is not cls:
        raise ValueError(f"type de fournisseur déjà enregistré : {kind}")
    _REGISTRY[kind] = cls
    return cls


def get_provider_class(kind: str) -> type[SearchProvider]:
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise SearchError(
            f"type de fournisseur inconnu : {kind!r} (types : {registered_types()})"
        ) from None


def registered_types() -> list[str]:
    return sorted(_REGISTRY)
