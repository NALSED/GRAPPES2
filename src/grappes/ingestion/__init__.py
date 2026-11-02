"""Ingestion : importer les modules déclenche l'enregistrement des fetchers."""

from grappes.ingestion import instagram, telegram, web, x  # noqa: F401
from grappes.ingestion.base import (
    BaseFetcher,
    FetcherError,
    RejectReport,
    get_fetcher,
    register,
    registered_ecosystems,
)

__all__ = [
    "BaseFetcher",
    "FetcherError",
    "RejectReport",
    "get_fetcher",
    "register",
    "registered_ecosystems",
]
