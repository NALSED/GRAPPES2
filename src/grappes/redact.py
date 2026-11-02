"""Masquage des personnes dans les sorties (`redact_persons`, D30).

Une entité de type `person` n'apparaît plus ni par son nom ni par son identifiant dans les CSV
d'export, `resultat.csv` et les réponses du serveur MCP : un pseudonyme stable la remplace. La
base interne garde les vraies valeurs (le scoring en a besoin) ; le masquage se fait aux frontières.
"""

from __future__ import annotations

import hashlib


def pseudonym(ref: str) -> str:
    """`x:compte_a` -> `x:personne-1a2b3c4d` (stable, non réversible sans la base)."""
    eco = ref.split(":", 1)[0]
    return f"{eco}:personne-{hashlib.sha256(ref.encode()).hexdigest()[:8]}"
