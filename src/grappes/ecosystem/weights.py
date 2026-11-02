"""Pondération des sources (config/sources.yaml)."""

from __future__ import annotations

from collections.abc import Callable

from grappes.config import SourcesConfig


def weight_fn(sources: SourcesConfig) -> Callable[[str], float]:
    """Retourne `clé canonique -> poids` (override > défaut de l'écosystème > 1.0)."""
    return sources.weight
