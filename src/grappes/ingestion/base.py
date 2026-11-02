"""Adaptateurs purs JSON brut -> Observations, et registre par écosystème."""

from __future__ import annotations

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from grappes.models import Observation

log = logging.getLogger(__name__)


class FetcherError(Exception):
    """JSON brut inexploitable."""


class Skip(Exception):
    """Enregistrement valide mais sans observation (ex. message de service) : compté, pas rejeté."""


@dataclass(slots=True)
class RejectReport:
    """Rapport de rejets d'un lot : comptes et motifs, sans interrompre le lot."""

    accepted: int = 0
    duplicates: int = 0
    ignored: int = 0
    rejected: list[tuple[int, str]] = field(default_factory=list)  # (index, motif), borné
    total_rejected: int = 0  # nombre réel de rejets, même au-delà de la liste détaillée
    MAX_DETAILS = 1000  # au-delà, seuls les comptes sont gardés (fichiers très gros)

    def reject(self, index: int, reason: str) -> None:
        self.total_rejected += 1
        if len(self.rejected) < self.MAX_DETAILS:
            self.rejected.append((index, reason))

    def absorb(self, other: RejectReport) -> None:
        """Cumule un lot dans ce rapport (ingestion par lots)."""
        self.accepted += other.accepted
        self.duplicates += other.duplicates
        self.ignored += other.ignored
        self.total_rejected += other.total_rejected
        room = self.MAX_DETAILS - len(self.rejected)
        self.rejected.extend(other.rejected[: max(room, 0)])

    @property
    def n_rejected(self) -> int:
        return self.total_rejected


class BaseFetcher(ABC):
    ecosystem: ClassVar[str]

    def configure(self, cfg: Any) -> BaseFetcher:
        """Reçoit la configuration (ex. table d'équivalence Telegram). Par défaut : rien à faire."""
        return self

    def finalize(self, observations: list[Observation], db: Any) -> list[Observation]:
        """Dernier traitement d'un lot avant stockage, avec accès à la base (homonymes...)."""
        return observations

    def unwrap(self, doc: Any) -> list[Any]:
        """Document JSON (liste, enveloppe d'export ou API) -> enregistrements à parser.

        Par défaut : un objet = un enregistrement. Les fetchers dont l'export a une enveloppe
        (`data`, `messages`...) la défont ici ; les enregistrements illisibles restent tels
        quels pour être comptés comme rejets.
        """
        return [doc]

    @abstractmethod
    def parse(self, raw: Mapping[str, Any]) -> Iterable[Observation]:
        """Un enregistrement JSON brut -> Observations. Pure, sans I/O."""

    def parse_many(
        self,
        raws: Iterable[Mapping[str, Any]],
        seen: set[str] | None = None,
        offset: int = 0,
    ) -> tuple[list[Observation], RejectReport]:
        """Tolérant : invalides comptés et journalisés, doublons (même obs_id) écartés.

        `seen` : identifiants déjà vus, partagé entre lots ; `offset` : rang du premier
        enregistrement du lot, pour des index de rejet continus."""
        report = RejectReport()
        seen = set() if seen is None else seen
        out: list[Observation] = []
        for i, raw in enumerate(raws, start=offset):
            try:
                obs_list = list(self.parse(raw))
            except Skip:
                report.ignored += 1
                continue
            except FetcherError as exc:
                report.reject(i, str(exc))
                log.warning("%s: enregistrement %d rejeté : %s", self.ecosystem, i, exc)
                continue
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                report.reject(i, f"{type(exc).__name__}: {exc}")
                log.warning("%s: enregistrement %d invalide : %s", self.ecosystem, i, exc)
                continue
            for obs in obs_list:
                if obs.obs_id in seen:
                    report.duplicates += 1
                    continue
                seen.add(obs.obs_id)
                out.append(obs)
                report.accepted += 1
        return out, report

    def fetch_raw(self, **params) -> Iterable[Mapping[str, Any]]:
        raise NotImplementedError("Hors périmètre v1 : le JSON est fourni de l'extérieur.")


_REGISTRY: dict[str, type[BaseFetcher]] = {}


def register(cls: type[BaseFetcher]) -> type[BaseFetcher]:
    """Décorateur : enregistre un fetcher par `ecosystem`."""
    eco = getattr(cls, "ecosystem", None)
    if not eco:
        raise ValueError(f"{cls.__name__} n'a pas d'attribut `ecosystem`")
    if eco in _REGISTRY and _REGISTRY[eco] is not cls:
        raise ValueError(f"écosystème déjà enregistré : {eco}")
    _REGISTRY[eco] = cls
    return cls


def get_fetcher(ecosystem: str) -> BaseFetcher:
    try:
        return _REGISTRY[ecosystem]()
    except KeyError:
        raise FetcherError(f"écosystème inconnu : {ecosystem}") from None


def registered_ecosystems() -> list[str]:
    return sorted(_REGISTRY)


# --- utilitaires partagés par les fetchers -------------------------------------------------

_HASHTAG_RE = re.compile(r"(?<!\w)#(\w+)", re.UNICODE)
_MENTION_RE = re.compile(r"(?<![\w@])@(\w{1,64})", re.UNICODE)


def make_obs_id(ecosystem: str, native_id: str, text: str) -> str:
    h = hashlib.sha256(f"{ecosystem}\x1f{native_id}\x1f{text}".encode()).hexdigest()
    return h[:24]


def parse_ts(value: Any) -> datetime | None:
    """ISO 8601 ou epoch (s) -> datetime UTC ; None si absent ou illisible."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, str) and value.strip().isdigit():  # epoch en chaîne
            value = int(value.strip())
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=UTC)
        dt = datetime.fromisoformat(str(value).strip())
    except (ValueError, OverflowError, OSError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def extract_hashtags(text: str) -> list[str]:
    return [m.casefold() for m in _HASHTAG_RE.findall(text)]


def extract_mentions(text: str) -> list[str]:
    return [m for m in _MENTION_RE.findall(text)]


def dedup(items: Iterable[Any]) -> tuple[Any, ...]:
    """Déduplication en conservant l'ordre."""
    return tuple(dict.fromkeys(items))


def to_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


Normalizer = Callable[[str], str]
