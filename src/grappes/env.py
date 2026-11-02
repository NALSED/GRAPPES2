"""Chargement des secrets : `config/secrets.env` -> variables d'environnement (sans dépendance).

Format : `NOM=valeur`, une par ligne ; `#` commence un commentaire ; guillemets facultatifs. Une variable
déjà définie dans l'environnement n'est jamais écrasée. Les valeurs ne sont ni retournées ni journalisées :
on ne retourne que les NOMS chargés. Le fichier doit rester hors dépôt (ignoré par git) et en droits 600.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_secrets(path: str | Path, warn=None) -> list[str]:
    """Charge le fichier s'il existe. Retourne les noms de variables définies par ce fichier."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o077:
            print(
                f"avertissement : {p} est lisible par d'autres (droits {mode:o}) ; chmod 600 recommandé",
                file=warn or sys.stderr,
            )
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    loaded = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, value = m.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if value and name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return loaded
