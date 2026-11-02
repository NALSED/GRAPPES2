"""Entités nommées d'un sujet (chantier 2, D63) : extraction depuis les textes archivés, fusion avec les candidats.

Les textes viennent de la base du rejeu (aucun réseau) ; l'extraction est archivée par (modèle, texte) : le modèle ne
tourne qu'une fois par texte. Une organisation nommée devient un candidat `ner:<nom normalisé>`, citée par les
sources qui l'ont nommée (arêtes source -> entité pour la découverte de vérité). Si son nom normalisé est celui d'un
candidat déjà connu (web ou registre), elle l'enrichit au lieu d'en créer un second.
"""

from __future__ import annotations

from pathlib import Path

from grappes.classement.candidats import Contexte
from grappes.classement.fusion import ajouter
from grappes.ner.extraire import NerConfig, Nomme, charger_config, extraire

CACHE = Path("donnees/autocal/ner")


def nommes_du_sujet(ctx: Contexte, cfg: NerConfig | None = None) -> dict[str, Nomme]:
    cfg = cfg or charger_config()
    pages = [(o.obs_id, o.source.key, o.text) for o in ctx.db.load_observations()]
    return extraire(pages, cfg, CACHE)



__all__ = ["CACHE", "ajouter", "nommes_du_sujet"]
