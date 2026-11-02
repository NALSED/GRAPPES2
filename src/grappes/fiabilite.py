"""Fiabilité des sources : code Admiralty (OTAN 6×6), poids par rang (méthode publiée), registres, Tranco.

Aucun barème inventé : la norme donne une échelle ORDONNÉE (A à F) ; les poids sont dérivés de l'ordre par la
méthode du « rank-order centroid » (Barron & Barrett, 1996), puis divisés par celui de la lettre de référence (D,
le web générique) pour que le comportement actuel — poids 1 — reste celui d'une source D.

La fiabilité s'ajoute au poids de `sources.yaml` (produit des deux) ; le recoupement entre au moins deux
écosystèmes reste une condition séparée et obligatoire (`ecosystem/crossref.py`).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from grappes.canon import canonical_domain

LETTERS = ("A", "B", "C", "D", "E", "F")
CREDIBILITY = {
    1: "Confirmée par d'autres sources",
    2: "Probablement vraie",
    3: "Possiblement vraie",
    4: "Douteuse",
    5: "Improbable",
    6: "Ne peut pas être jugée",
}
TRANCO_SIZE = 1_000_000  # taille de la liste Tranco (top 1 million)


class FiabiliteError(ValueError):
    """Table de fiabilité invalide."""


def roc_weights(n: int) -> list[float]:
    """Poids « rank-order centroid » des rangs 1..n (somme 1) : w_i = (1/n)·Σ_{k=i..n} 1/k."""
    return [sum(1 / k for k in range(i, n + 1)) / n for i in range(1, n + 1)]


def letter_factors(reference: str = "D") -> dict[str, float]:
    """Facteur de chaque lettre, relatif à la lettre de référence (facteur 1)."""
    w = roc_weights(len(LETTERS))
    ref = w[LETTERS.index(reference)]
    return {letter: w[i] / ref for i, letter in enumerate(LETTERS)}


@dataclass(frozen=True, slots=True)
class Fiabilite:
    enabled: bool = (
        False  # faux : pondération neutre (tous les facteurs valent 1), comme avant l'Admiralty
    )
    factors: dict[str, float] = field(default_factory=lambda: dict.fromkeys(LETTERS, 1.0))
    ecosystems: dict[str, str] = field(default_factory=dict)
    default: str = "F"
    media: tuple[str, ...] = ()
    digest: str = ""  # empreinte du fichier (entre dans `config_hash`)
    tranco: dict[str, Any] = field(
        default_factory=dict
    )  # {dossier, cache_jours, api} : affinage des sources D

    def letter_static(self, ref: str) -> str:
        """Lettre d'après l'écosystème et la liste de médias établis (ni registre, ni Tranco)."""
        eco, _, ident = ref.partition(":")
        if eco in ("web", "rss"):
            host = canonical_domain(ident)
            if any(host == m or host.endswith("." + m) for m in self.media):
                return "B"
        return self.ecosystems.get(eco, self.default)


def load_fiabilite(path: Path) -> Fiabilite:
    """`config/fiabilite_sources.yaml`. Absent : pondération neutre (comportement historique)."""
    if not path.exists():
        return Fiabilite()
    try:
        raw = path.read_bytes()
        d = yaml.safe_load(raw) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise FiabiliteError(f"{path}: {exc}") from exc
    if not isinstance(d, dict):
        raise FiabiliteError(f"{path}: un objet YAML est attendu")
    poids = d.get("poids") or {}
    if poids.get("methode", "rang_centroide") != "rang_centroide":
        raise FiabiliteError(f"{path}: seule la méthode `rang_centroide` est connue")
    ref = str(poids.get("reference", "D"))
    if ref not in LETTERS:
        raise FiabiliteError(f"{path}: lettre de référence inconnue {ref!r}")
    eco = d.get("ecosystemes") or {}
    if not isinstance(eco, dict) or any(v not in LETTERS for v in eco.values()):
        raise FiabiliteError(
            f"{path}: `ecosystemes` doit associer un écosystème à une lettre A à F"
        )
    default = str(d.get("defaut", "F"))
    if default not in LETTERS:
        raise FiabiliteError(f"{path}: `defaut` doit être une lettre A à F")
    media = d.get("medias_etablis") or []
    if not isinstance(media, list) or not all(isinstance(m, str) and m.strip() for m in media):
        raise FiabiliteError(f"{path}: `medias_etablis` doit être une liste de domaines")
    tranco = d.get("tranco") or {}
    if not isinstance(tranco, dict):
        raise FiabiliteError(f"{path}: `tranco` doit être un mapping")
    return Fiabilite(
        enabled=True,
        tranco={
            "dossier": str(tranco.get("dossier", "donnees/tranco")),
            "cache_jours": int(tranco.get("cache_jours", 30)),
            "api": str(tranco.get("api", "https://tranco-list.eu/api/lists/date/{date}")),
            "actif": bool(tranco.get("actif", False)),
        },
        factors=letter_factors(ref),
        ecosystems={str(k): str(v) for k, v in eco.items()},
        default=default,
        media=tuple(canonical_domain(m) for m in media),
        digest=hashlib.sha256(raw).hexdigest()[:16],
    )


@dataclass(slots=True)
class Reliability:
    """Fiabilité à l'exécution : la table, plus les correspondances de registres (A) et les rangs Tranco."""

    table: Fiabilite
    registre: frozenset[str] = (
        frozenset()
    )  # clés de sources dont un registre officiel confirme l'existence (A)
    tranco: dict[str, int] = field(default_factory=dict)  # domaine -> rang
    tranco_id: str = ""  # identifiant/date de la liste Tranco utilisée (traçabilité)

    def letter(self, ref: str) -> str:
        if not self.table.enabled:
            return "D"
        if ref in self.registre:
            return "A"
        return self.table.letter_static(ref)

    def factor(self, ref: str) -> float:
        """Facteur de fiabilité. Pour une source D du web, le rang Tranco l'affine entre D (absent du classement) et
        C (jamais plus haut : une source D ne dépasse pas le niveau de la lettre juste au-dessus)."""
        if not self.table.enabled:
            return 1.0
        letter = self.letter(ref)
        f = self.table.factors[letter]
        if letter == "D" and self.tranco and ref.partition(":")[0] in ("web", "rss"):
            rank = self.tranco.get(canonical_domain(ref.partition(":")[2]))
            if rank:
                s = max(0.0, min(1.0, 1 - math.log10(rank) / math.log10(TRANCO_SIZE)))
                f = f + (self.table.factors["C"] - f) * s
        return f

    def text_factor(self, ref: str) -> float:
        """Facteur d'une MENTION dans un texte libre (D54) : la lettre juste en dessous de celle de la source (une
        mention pèse moins qu'un lien explicite) ; pas de Tranco, pas de création de barème : mêmes facteurs ROC."""
        if not self.table.enabled:
            return 1.0
        i = LETTERS.index(self.letter(ref))
        return self.table.factors[LETTERS[min(i + 1, len(LETTERS) - 1)]]

    @property
    def fingerprint(self) -> str:
        """Empreinte de ce qui change les poids hors configuration (registres, Tranco) : entre dans le `run_id`."""
        h = hashlib.sha256()
        h.update(self.table.digest.encode())
        h.update(",".join(sorted(self.registre)).encode())
        h.update(self.tranco_id.encode())
        return h.hexdigest()[:12]


def neutral() -> Reliability:
    return Reliability(Fiabilite())


def explain(r: Reliability, ref: str) -> dict[str, Any]:
    """Pour l'explicabilité : lettre, facteur et raison."""
    letter = r.letter(ref)
    why = (
        "registre officiel" if ref in r.registre
        else "média établi (liste nommée)" if letter == "B"
        else f"type de fournisseur « {ref.partition(':')[0]} »"
    )  # fmt: skip
    return {"fiabilite": letter, "facteur": round(r.factor(ref), 3), "raison": why}
