"""Fichiers « appris » par la boucle automatique (phase 9), séparés de la configuration de Sednal.

`config/apprentissage/lexique_appris_<grappe>.yaml` : termes ajoutés à une grappe (poids faible,
expiration). `config/apprentissage/seuils_appris.yaml` : seuils recalibrés dans des bornes.
Les lexiques et ancres écrits par Sednal (`config/grappes/*.yaml`) ne sont jamais modifiés : un terme
appris ne remplace jamais un terme de Sednal. Ces fichiers entrent dans `config_hash` : un run reste
rejouable à l'identique.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

APPRIS_DIR = "apprentissage"
_SAFE = re.compile(r"[^\w.-]")
THRESHOLD_KEYS = ("seuil_min", "marge_ambiguite")


class LearnedError(ValueError):
    """Fichier appris illisible ou invalide."""


@dataclass(frozen=True, slots=True)
class LearnedTerm:
    weight: float
    added: int  # numéro du cycle d'ajout
    seen: int  # dernier cycle où le terme a été revu


def learned_dir(config_dir: str | Path) -> Path:
    return Path(config_dir) / APPRIS_DIR


def canonical_digest(path: Path) -> bytes:
    """Contenu qui compte pour le scoring dans un fichier appris (termes et poids, seuils), sans les
    numéros de cycle : revoir un terme ne change pas `config_hash`, donc pas le `run_id`."""
    import json

    data = _read(path)
    if "termes" in data:
        core = sorted(
            (t, round(float(s.get("poids", 0)), 4)) for t, s in (data["termes"] or {}).items()
        )
    else:
        core = sorted((k, round(float(v), 4)) for k, v in data.items() if k in THRESHOLD_KEYS)
    return json.dumps(core, ensure_ascii=False).encode()


def lexicon_path(config_dir: str | Path, grappe_id: str) -> Path:
    return learned_dir(config_dir) / f"lexique_appris_{_SAFE.sub('_', grappe_id)}.yaml"


def seed_path(config_dir: str | Path, grappe_id: str) -> Path:
    """Lexique d'amorçage d'un sujet : `config/apprentissage/amorcage_<grappe>.yaml`."""
    return learned_dir(config_dir) / f"amorcage_{_SAFE.sub('_', grappe_id)}.yaml"


def thresholds_path(config_dir: str | Path) -> Path:
    return learned_dir(config_dir) / "seuils_appris.yaml"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LearnedError(f"{path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise LearnedError(f"{path}: un objet YAML est attendu")
    return data


def _num(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LearnedError(f"{where}: nombre fini attendu ({value!r})")
    return float(value)


def load_seed(config_dir: str | Path, grappe_id: str) -> dict[str, LearnedTerm]:
    """Termes d'amorçage d'un sujet (extraits des premiers résultats de recherche)."""
    return _load_terms(seed_path(config_dir, grappe_id))


def save_seed(config_dir: str | Path, grappe_id: str, terms: dict[str, LearnedTerm]) -> Path | None:
    return _save_terms(seed_path(config_dir, grappe_id), grappe_id, terms)


def load_learned_lexicon(config_dir: str | Path, grappe_id: str) -> dict[str, LearnedTerm]:
    return _load_terms(lexicon_path(config_dir, grappe_id))


def _load_terms(path: Path) -> dict[str, LearnedTerm]:
    if not path.exists():
        return {}
    raw = _read(path).get("termes") or {}
    if not isinstance(raw, dict):
        raise LearnedError(f"{path}: `termes` doit être un mapping")
    out: dict[str, LearnedTerm] = {}
    for term, spec in raw.items():
        if not isinstance(spec, dict) or not isinstance(term, str) or not term.strip():
            raise LearnedError(f"{path}: terme invalide {term!r}")
        weight = _num(spec.get("poids"), f"{path}:{term}.poids")
        if weight <= 0:
            raise LearnedError(f"{path}:{term}: poids > 0 attendu")
        out[term] = LearnedTerm(
            weight, int(_num(spec.get("ajoute_cycle", 0), f"{path}:{term}")),
            int(_num(spec.get("vu_cycle", 0), f"{path}:{term}")),
        )  # fmt: skip
    return out


def save_learned_lexicon(
    config_dir: str | Path, grappe_id: str, terms: dict[str, LearnedTerm]
) -> Path | None:
    """Écrit le lexique appris (ou supprime le fichier s'il est vide). Tri stable."""
    return _save_terms(lexicon_path(config_dir, grappe_id), grappe_id, terms)


def _save_terms(path: Path, grappe_id: str, terms: dict[str, LearnedTerm]) -> Path | None:
    if not terms:
        path.unlink(missing_ok=True)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "grappe": grappe_id,
        "termes": {
            t: {"poids": round(v.weight, 4), "ajoute_cycle": v.added, "vu_cycle": v.seen}
            for t, v in sorted(terms.items())
        },
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def load_learned_thresholds(config_dir: str | Path) -> dict[str, float]:
    path = thresholds_path(config_dir)
    if not path.exists():
        return {}
    data = _read(path)
    out = {}
    for key in THRESHOLD_KEYS:
        if key in data:
            value = _num(data[key], f"{path}:{key}")
            if not 0 <= value <= 100:
                raise LearnedError(f"{path}:{key} doit être entre 0 et 100")
            out[key] = value
    return out


INGESTION_KEY = "seuil_ingestion"  # seuil d'ingestion auto-ajusté (n'entre pas dans `config_hash`)


def load_ingestion_threshold(config_dir: str | Path) -> float | None:
    """Dernier seuil d'ingestion ajusté par la boucle, ou `None`."""
    path = thresholds_path(config_dir)
    if not path.exists():
        return None
    data = _read(path)
    if INGESTION_KEY not in data:
        return None
    return _num(data[INGESTION_KEY], f"{path}:{INGESTION_KEY}")


def save_ingestion_threshold(config_dir: str | Path, value: float, cycle: int) -> Path:
    """Écrit le seuil d'ingestion sans toucher aux autres seuils appris du fichier."""
    path = thresholds_path(config_dir)
    data = _read(path) if path.exists() else {}
    data[INGESTION_KEY] = round(float(value), 4)
    data["cycle"] = cycle
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def save_learned_thresholds(config_dir: str | Path, values: dict[str, float], cycle: int) -> Path:
    path = thresholds_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists() and INGESTION_KEY in _read(path):  # conserve le seuil d'ingestion déjà ajusté
        data[INGESTION_KEY] = _read(path)[INGESTION_KEY]
    data.update({k: round(float(v), 4) for k, v in values.items() if k in THRESHOLD_KEYS})
    data["cycle"] = cycle
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path
