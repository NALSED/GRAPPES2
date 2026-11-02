"""Compatibilité : le tableau des candidats vit désormais dans `grappes.classement.candidats` (production)."""

from grappes.classement.candidats import Candidat, Contexte, charger, domaine

__all__ = ["Candidat", "Contexte", "charger", "domaine"]
