"""Fetcher web : format proposé (`links` avec `href`) et variante historique (`outlinks`).

Formats : voir docs/conception/formats_json.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from grappes.canon import canonical_domain, canonical_url
from grappes.ingestion.base import (
    BaseFetcher,
    FetcherError,
    dedup,
    make_obs_id,
    parse_ts,
    register,
)
from grappes.models import Observation, SourceRef


@register
class WebFetcher(BaseFetcher):
    ecosystem = "web"

    def parse(self, raw: Mapping[str, Any]) -> Iterable[Observation]:
        if not isinstance(raw, Mapping):
            raise FetcherError("enregistrement non objet")
        url = next(
            (
                raw[k]
                for k in ("canonical_url", "url")
                if isinstance(raw.get(k), str) and canonical_domain(canonical_url(raw[k]))
            ),
            None,
        )
        if url is None:
            raise FetcherError("url manquante")
        page_url = canonical_url(url)
        domain = canonical_domain(page_url)
        title = raw.get("title") if isinstance(raw.get("title"), str) else ""
        body = raw.get("text") if isinstance(raw.get("text"), str) else ""
        text = f"{title}\n{body}".strip()
        hrefs: list[str] = []
        entries: list[Any] = []
        for key in ("links", "outlinks"):
            if isinstance(raw.get(key), list):
                entries.extend(raw[key])
        for entry in entries:
            href = entry.get("href") if isinstance(entry, Mapping) else entry
            if isinstance(href, str) and href.strip():
                hrefs.append(canonical_url(href))
        links = hrefs
        yield Observation(
            obs_id=make_obs_id("web", page_url, text),
            source=SourceRef("web", domain),
            ts=parse_ts(raw.get("published_at")) or parse_ts(raw.get("fetched_at")),
            text=text,
            links=dedup(u for u in links if u and canonical_domain(u) != domain),
            raw_ref=page_url,
            meta={
                "page_url": page_url,
                **({"lang": str(raw["lang"])} if raw.get("lang") else {}),
                **(
                    {"nom": raw["site_name"].strip()}
                    if isinstance(raw.get("site_name"), str) and raw["site_name"].strip()
                    else {}
                ),
            },
        )
