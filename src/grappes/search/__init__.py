"""Recherche de candidats : « multiprise » à fournisseurs enfichables (phase 8).

Le cœur ne connaît aucun fournisseur. Un fournisseur est : un fichier YAML (types génériques
`http` et `mcp`), ou un plugin Python déposé dans `plugins/`. Les résultats sont des données
non fiables : seuls des champs typés et nettoyés en sortent.
"""

from grappes.search.base import (
    ProviderStop,
    ProviderUnavailable,
    QuotaExceeded,
    SearchContext,
    SearchError,
    SearchProvider,
    get_provider_class,
    register_provider,
    registered_types,
)
from grappes.search.models import Candidate, MergedCandidate

__all__ = [
    "Candidate",
    "MergedCandidate",
    "ProviderStop",
    "ProviderUnavailable",
    "QuotaExceeded",
    "SearchContext",
    "SearchError",
    "SearchProvider",
    "get_provider_class",
    "register_provider",
    "registered_types",
]
