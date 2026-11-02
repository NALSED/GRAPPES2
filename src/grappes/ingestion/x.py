"""Fetcher X : réponse API v2 (`data` + `includes.users`) et enregistrement plat historique.

Formats : voir docs/conception/formats_json.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from grappes.canon import canonical_handle, canonical_url
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
class XFetcher(BaseFetcher):
    ecosystem = "x"

    def unwrap(self, doc: Any) -> list[Any]:
        """Réponse API v2 `{data, includes: {users}, meta}` -> un enregistrement par tweet.

        L'auteur n'est donné que par `author_id` : il est résolu en `username` grâce à
        `includes.users`, sans aucune requête externe ni ré-identification. Les mentions sans
        `username` sont résolues de la même façon (par `id`) quand c'est possible.
        """
        if not (isinstance(doc, Mapping) and isinstance(doc.get("data"), (list, Mapping))):
            return [doc]
        data = doc["data"]
        tweets = data if isinstance(data, list) else [data]
        inc = doc.get("includes") if isinstance(doc.get("includes"), Mapping) else {}
        user_rows = [
            u
            for u in inc.get("users") or []
            if isinstance(u, Mapping) and u.get("id") and isinstance(u.get("username"), str)
        ]
        users = {str(u["id"]): u["username"] for u in user_rows}
        full_names = {str(u["id"]): u["name"] for u in user_rows if isinstance(u.get("name"), str)}
        out: list[Any] = []
        for t in tweets:
            if not isinstance(t, Mapping):
                out.append(t)
                continue
            t = dict(t)
            username = users.get(str(t.get("author_id", "")))
            if "author" not in t and username:
                author = {"handle": username}
                if str(t.get("author_id", "")) in full_names:
                    author["name"] = full_names[str(t["author_id"])]
                t["author"] = author
            ent = t.get("entities")
            if isinstance(ent, Mapping) and isinstance(ent.get("mentions"), list):
                mentions = []
                for m in ent["mentions"]:
                    if isinstance(m, Mapping) and not (m.get("username") or m.get("handle")):
                        name = users.get(str(m.get("id", "")))
                        m = {**m, "username": name} if name else m
                    mentions.append(m)
                t["entities"] = {**ent, "mentions": mentions}
            out.append(t)
        return out

    def parse(self, raw: Mapping[str, Any]) -> Iterable[Observation]:
        if not isinstance(raw, Mapping):
            raise FetcherError("enregistrement non objet")
        author = raw.get("author")
        handle = author.get("handle") if isinstance(author, Mapping) else author
        if not isinstance(handle, str) or not canonical_handle(handle):
            if raw.get("author_id"):
                raise FetcherError("auteur introuvable (author_id absent de includes.users)")
            raise FetcherError("auteur manquant")
        native_id = str(raw.get("id", ""))
        text = raw.get("text")
        text = text if isinstance(text, str) else ""
        if not native_id and not text:
            raise FetcherError("ni id ni texte")

        ent = raw.get("entities") if isinstance(raw.get("entities"), Mapping) else {}
        mention_handles = [
            (m.get("username") or m.get("handle") or "") if isinstance(m, Mapping) else str(m)
            for m in ent.get("mentions") or []
        ] or extract_mentions(text)
        urls = []
        for u in ent.get("urls") or []:
            url = (u.get("expanded_url") or u.get("url")) if isinstance(u, Mapping) else u
            if isinstance(url, str) and url.strip():
                urls.append(canonical_url(url))
        tags = [
            str((t.get("tag") or t.get("hashtag") or "") if isinstance(t, Mapping) else t)
            .lstrip("#")
            .casefold()
            for t in ent.get("hashtags") or []
        ] or extract_hashtags(text)

        metrics = next(
            (raw[k] for k in ("public_metrics", "metrics") if isinstance(raw.get(k), Mapping)), {}
        )
        engagement = sum(
            to_int(metrics.get(new, metrics.get(old)))
            for new, old in (
                ("like_count", "likes"),
                ("retweet_count", "retweets"),
                ("reply_count", "replies"),
            )
        )
        source = SourceRef("x", canonical_handle(handle))
        meta = {"lang": str(raw["lang"])} if raw.get("lang") else {}
        if (
            isinstance(author, Mapping)
            and isinstance(author.get("name"), str)
            and author["name"].strip()
        ):
            meta["nom"] = author["name"].strip()
        yield Observation(
            obs_id=make_obs_id("x", native_id, text),
            source=source,
            ts=parse_ts(raw.get("created_at")),
            text=text,
            mentions=dedup(SourceRef("x", h) for h in map(canonical_handle, mention_handles) if h),
            links=dedup(u for u in urls if u),
            hashtags=dedup(t for t in tags if t),
            engagement=engagement,
            raw_ref=native_id or None,
            meta=meta,
        )
