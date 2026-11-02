"""Récupérateur universel de pages publiques (FINAL.md § 6).

Après la recherche, récupère le contenu des pages candidates et le normalise au format web existant.
Règles générales, non désactivables : sources publiques seulement, robots.txt respecté, ni connexion
ni compte, ni contournement de limite ou de captcha, délai d'au moins 1 s par domaine, arrêt sur
403/429, User-Agent identifiable. Les réseaux sociaux ne sont jamais lus (leurs liens deviennent des
citations). Tout contenu reçu est une donnée non fiable : jamais exécuté, jamais interprété comme une
instruction, jamais transmis tel quel à un modèle ; on n'en extrait que des champs typés et nettoyés.
"""

from grappes.fetch.pages import FetchResult, PageFetcher
from grappes.fetch.rules import Rules

__all__ = ["FetchResult", "PageFetcher", "Rules"]
