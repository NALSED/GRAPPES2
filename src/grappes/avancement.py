"""Rapport d'avancement (D70) : grappes dit où il en est pendant qu'il travaille.

Inactif par défaut. Si la variable d'environnement `GRAPPES_AVANCEMENT` donne un chemin de fichier, chaque événement y est
ajouté sous forme d'une ligne JSON (`{"t": ..., "evenement": ..., ...}`) : source en cours, appels faits sur appels
prévus, noms collectés, étape (recherche, vérification, sortie). Un lecteur extérieur (pilote) suit ce fichier.

Garanties : jamais d'exception (un rapport qui échoue ne doit jamais faire échouer un cycle), jamais de secret (seulement
des noms de prises, des compteurs et des étapes), aucun effet sur la ligne de commande ni sur les résultats.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

VARIABLE = "GRAPPES_AVANCEMENT"


def signaler(evenement: str, **champs: Any) -> None:
    chemin = os.environ.get(VARIABLE)
    if not chemin:
        return
    try:
        ligne = json.dumps({"t": round(time.time(), 3), "evenement": evenement, **champs}, ensure_ascii=False, default=str)
        with open(chemin, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except (OSError, TypeError, ValueError):
        pass
