"""Configuration de la recherche : liste blanche des fournisseurs, un fichier YAML par fournisseur.

`config/recherche.yaml` : réglages globaux. `config/recherche/*.yaml` : un fournisseur par fichier
(les sous-dossiers, dont `exemples/`, ne sont pas chargés). Les appels réels dépendent du seul
interrupteur `autonome` (`config/autonomie.yaml`) ; sans lui, tout est dry-run.
Cette configuration est indépendante de `config_hash` : elle ne change aucun score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from grappes.config import ConfigError


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str
    type: str
    options: dict[str, Any]
    public: bool = True  # source publique, sans identifiant de connexion (exigence de la phase 9)
    path: Path | None = None


@dataclass(frozen=True, slots=True)
class SearchConfig:
    actifs: tuple[str, ...] = ()  # liste blanche : vide = tout désactivé
    quota_total: int = 100
    delai_s: float = 1.0
    limite_par_mot_cle: int = 20
    plugins_dir: Path = Path("plugins")
    journal: Path = Path("donnees/journal_recherche.log")
    providers: dict[str, ProviderSpec] = field(default_factory=dict)


def _yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: un objet YAML est attendu")
    return data


def _num(d: dict[str, Any], key: str, default: float, path: Path, *, minimum: float = 0) -> float:
    try:
        value = float(d.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: {key} doit être un nombre") from exc
    if not math.isfinite(value) or value < minimum:
        raise ConfigError(f"{path}: {key} doit être un nombre fini >= {minimum:g}")
    return value


def load_search_config(config_dir: str | Path = "config") -> SearchConfig:
    root = Path(config_dir)
    project = root.resolve().parent
    gpath = root / "recherche.yaml"
    d = _yaml(gpath) if gpath.exists() else {}
    actifs = d.get("actifs") or []
    if not isinstance(actifs, list) or not all(isinstance(a, str) for a in actifs):
        raise ConfigError(f"{gpath}: `actifs` doit être une liste d'identifiants")
    providers: dict[str, ProviderSpec] = {}
    pdir = root / "recherche"
    for p in sorted(pdir.glob("*.yaml")) if pdir.is_dir() else []:
        spec = _yaml(p)
        pid, kind = spec.pop("id", None), spec.pop("type", None)
        if not isinstance(pid, str) or not pid.strip() or not isinstance(kind, str) or not kind:
            raise ConfigError(f"{p}: `id` et `type` sont obligatoires")
        if pid in providers:
            raise ConfigError(f"{p}: fournisseur dupliqué {pid!r}")
        public = spec.pop("public", True)
        if not isinstance(public, bool):
            raise ConfigError(f"{p}: `public` doit être vrai ou faux")
        providers[pid] = ProviderSpec(pid, kind, spec, public, p)
    unknown = [a for a in actifs if a not in providers]
    if unknown:
        raise ConfigError(f"{gpath}: fournisseurs actifs sans fichier de configuration : {unknown}")
    return SearchConfig(
        actifs=tuple(dict.fromkeys(actifs)),
        quota_total=int(_num(d, "quota_total", 100, gpath, minimum=0)),
        delai_s=_num(d, "delai_s", 1.0, gpath),
        limite_par_mot_cle=int(_num(d, "limite_par_mot_cle", 20, gpath, minimum=1)),
        plugins_dir=project / str(d.get("plugins_dir", "plugins")),
        journal=project / str(d.get("journal", "donnees/journal_recherche.log")),
        providers=providers,
    )
