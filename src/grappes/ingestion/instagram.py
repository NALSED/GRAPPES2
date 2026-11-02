"""Fetcher Instagram : liste de médias de l'API Graph (`data` + `paging`).

Formats : voir docs/conception/formats_json.md. Aucun accès réseau : le JSON est fourni.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from grappes.canon import canonical_handle, canonical_url, extract_urls
from grappes.ingestion.base import (
    BaseFetcher,
    FetcherError,
    dedup,
    extract_hashtags,
    extract_mentions,
    make_obs_id,
    parse_ts,
    register,
    to_int,
)
from grappes.models import Observation, SourceRef


@register
class InstagramFetcher(BaseFetcher):
    ecosystem = "instagram"

    def unwrap(self, doc: Any) -> list[Any]:
        """`{data: [médias], paging: {...}}` -> un enregistrement par média ; `paging` est
        ignoré (la pagination relève de la collecte, hors périmètre)."""
        if isinstance(doc, Mapping) and isinstance(doc.get("data"), (list, Mapping)):
            data = doc["data"]
            return list(data) if isinstance(data, list) else [data]
        return [doc]

    def parse(self, raw: Mapping[str, Any]) -> Iterable[Observation]:
        if not isinstance(raw, Mapping):
            raise FetcherError("enregistrement non objet")
        username = raw.get("username")
        if not isinstance(username, str) or not canonical_handle(username):
            raise FetcherError("username manquant")
        native_id = str(raw.get("id", ""))
        caption = raw.get("caption")
        text = caption if isinstance(caption, str) else ""
        if not native_id and not text:
            raise FetcherError("ni identifiant ni légende")
        source = SourceRef("instagram", canonical_handle(username))
        links = [canonical_url(u) for u in extract_urls(text)]
        meta = {"media_type": str(raw["media_type"])} if raw.get("media_type") else {}
        yield Observation(
            obs_id=make_obs_id("instagram", native_id, text),
            source=source,
            ts=parse_ts(raw.get("timestamp")),
            text=text,
            mentions=dedup(
                SourceRef("instagram", h)
                for h in map(canonical_handle, extract_mentions(text))
                if h
            ),
            links=dedup(u for u in links if u),
            hashtags=dedup(extract_hashtags(text)),
            engagement=to_int(raw.get("like_count")) + to_int(raw.get("comments_count")),
            raw_ref=str(raw["permalink"]) if raw.get("permalink") else native_id or None,
            meta=meta,
        )
