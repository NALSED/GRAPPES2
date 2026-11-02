"""Découverte automatique des plugins Python : tout `plugins/*.py` est chargé.

Un plugin définit une sous-classe de `SearchProvider` décorée par `@register_provider`, puis
un fichier YAML l'active (`type: <type du plugin>`). Aucun fichier du dépôt n'est modifié.
Un plugin est du code local écrit par l'opérateur : il s'exécute avec ses droits, il n'est
jamais téléchargé. Une erreur dans un plugin est journalisée et ce plugin est ignoré.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def discover_plugins(plugins_dir: Path) -> list[str]:
    """Charge les `*.py` du dossier (hors `_*`). Retourne les noms de fichiers chargés."""
    loaded: list[str] = []
    if not plugins_dir.is_dir():
        return loaded
    for path in sorted(plugins_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = f"grappes_plugin_{path.stem}"
        if name in sys.modules:
            loaded.append(path.name)
            continue
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ImportError("module illisible")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            loaded.append(path.name)
        except Exception as exc:  # noqa: BLE001 - un plugin défaillant ne doit pas arrêter le cycle
            sys.modules.pop(name, None)
            log.warning("plugin %s ignoré : %s: %s", path.name, type(exc).__name__, exc)
    return loaded
