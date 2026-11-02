"""Profils de sortie configurables (D64) : une liste de lignes (`LigneSortie`) -> un CSV par profil actif.

Le contenu du fichier est décidé par `config/sortie.yaml` (colonnes, ordre, en-tête, séparateur), pas par le code.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from grappes.clustering.export import safe_cell
from grappes.config import ConfigError

COLONNES = ("nom", "ville", "pays", "score", "bande", "calibre", "sources", "registres", "statut")


@dataclass(slots=True)
class LigneSortie:
    nom: str
    ville: str = ""
    pays: str = ""
    score: float | None = None  # 0 à 100
    bande: str = ""
    calibre: str = ""  # « mesuré » | « estimation étendue » | « non calibré »
    sources: list[str] = field(default_factory=list)
    registres: list[str] = field(default_factory=list)
    statut: str = ""

    def valeur(self, colonne: str) -> str:
        v = getattr(self, colonne)
        if colonne == "score":
            return "" if v is None else f"{v:.0f}"
        if isinstance(v, list):
            return "|".join(v)
        return str(v)


@dataclass(frozen=True, slots=True)
class Profil:
    id: str
    actif: bool
    fichier: str
    colonnes: tuple[str, ...]
    entete: tuple[str, ...]
    delimiteur: str


def charger_profils(config_dir: str | Path) -> list[Profil]:
    """`config/sortie.yaml`. Absent : les deux profils historiques (resultat, tsites)."""
    f = Path(config_dir) / "sortie.yaml"
    if not f.exists():
        return [
            Profil("resultat", True, "resultat.csv", ("nom",), ("NOM",), ","),
            Profil("tsites", True, "resultat_tsites.csv", ("nom", "ville", "pays"), ("nom", "ville", "pays"), ";"),
        ]
    y = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    out = []
    for pid, d in (y.get("profils") or {}).items():
        cols = tuple(d.get("colonnes", ()))
        inconnues = [c for c in cols if c not in COLONNES]
        if inconnues:
            raise ConfigError(f"{f}: profil {pid}: colonnes inconnues {inconnues} (disponibles : {', '.join(COLONNES)})")
        out.append(Profil(pid, bool(d.get("actif", True)), str(d["fichier"]), cols, tuple(d.get("entete", cols)), str(d.get("delimiteur", ","))))
    return out


def ecrire(lignes: list[LigneSortie], profils: list[Profil], out_dir: str | Path, ignorer: frozenset[str] = frozenset()) -> list[Path]:
    """Écrit chaque profil actif (sauf `ignorer`, déjà écrits ailleurs) ; supprime le fichier d'un profil désactivé."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for p in profils:
        cible = out / p.fichier
        if not p.actif:
            if p.id not in ignorer:
                cible.unlink(missing_ok=True)  # pas de fichier périmé
            continue
        if p.id in ignorer:
            continue
        with cible.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=p.delimiteur, lineterminator="\n")
            w.writerow(p.entete)
            for ligne in lignes:
                w.writerow([safe_cell(ligne.valeur(c)) for c in p.colonnes])
        paths.append(cible)
    return paths
