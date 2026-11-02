"""Fetcher Telegram : export Telegram Desktop (`messages`) et enregistrement plat historique.

Identité d'un canal (D25, `FINAL.md` § 11). L'export ne donne que le nom affiché. Référence : la
table d'équivalence de configuration (`config/telegram.yaml`, nom affiché -> identifiant public).
Repli si le nom n'y est pas : dérivation du nom (minuscules, accents ôtés, espaces -> `_`),
marquée « hypothèse » : les citations qui l'impliquent sont conservées et signalées mais ne
valident jamais un recoupement. Deux canaux de même nom affiché ne sont jamais fusionnés.

Formats : voir docs/conception/formats_json.md.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping
from typing import Any

from grappes.canon import canonical_handle, canonical_url
from grappes.ingestion.base import (
    BaseFetcher,
    FetcherError,
    Skip,
    dedup,
    extract_hashtags,
    extract_mentions,
    make_obs_id,
    parse_ts,
    register,
    to_int,
)
from grappes.models import Observation, SourceRef
from grappes.semantic.scoring import normalize

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_BARE_ID = re.compile(r"^[A-Za-z0-9_]{3,}$")


def name_key(name: str) -> str:
    """Clé de recherche d'un nom affiché : casse, accents latins, ponctuation et espaces ôtés."""
    return _NON_WORD.sub(" ", normalize(name)).strip()


def derive_ident(name: str) -> str:
    """Identifiant déduit d'un nom affiché : `Café Crème Info` -> `cafe_creme_info` (hypothèse)."""
    return _NON_WORD.sub("_", normalize(name)).strip("_")


def resolve_name(name: str, table: Mapping[str, str]) -> tuple[str, bool]:
    """(identifiant, exact) d'un canal désigné par son nom affiché.

    Exact : `@nom_public` ou nom présent dans la table. Sinon : dérivation, `exact = False`.
    """
    name = name.strip()
    if name.startswith("@"):
        return canonical_handle(name), True
    found = table.get(name_key(name))
    if found:
        return found, True
    return derive_ident(name), False


def _flatten(text: Any) -> str:
    """`text` de l'export : chaîne, ou liste de chaînes et d'objets `{type, text}`."""
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "".join(
            p if isinstance(p, str) else str(p.get("text", ""))
            for p in text
            if isinstance(p, (str, Mapping))
        )
    return ""


@register
class TelegramFetcher(BaseFetcher):
    ecosystem = "telegram"

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def configure(self, cfg: Any) -> TelegramFetcher:
        self.names = dict(getattr(cfg, "telegram_names", {}) or {})
        return self

    def unwrap(self, doc: Any) -> list[Any]:
        """Export d'un canal : `{name, type, id, messages: [...]}` -> un enregistrement par
        message, le canal étant celui de l'export (résolu par la table, sinon dérivé)."""
        if not (isinstance(doc, Mapping) and isinstance(doc.get("messages"), list)):
            return [doc]
        username = doc.get("username")
        name = doc.get("name")
        if isinstance(username, str) and canonical_handle(username):
            channel, exact = canonical_handle(username), True
        elif isinstance(name, str) and derive_ident(name):
            channel, exact = resolve_name(name, self.names)
        else:
            channel, exact = "", False
        extra = {
            "channel": channel,
            "_ident_exact": exact,
            "_chat_name": name,
            "_chat_id": doc.get("id"),
        }
        return [
            {**m, **extra} if isinstance(m, Mapping) and channel else m for m in doc["messages"]
        ]

    def finalize(self, observations: list[Observation], db: Any) -> list[Observation]:
        """Deux canaux de même nom affiché (identifiants natifs différents) ne sont pas fusionnés :
        le second reçoit l'identifiant `<déduit>~<id natif>`."""
        known = db.native_ids() if hasattr(db, "native_ids") else {}
        seen: dict[str, str] = {}
        out: list[Observation] = []
        for o in observations:
            native = o.meta.get("chat_id")
            key = o.source.key
            if native and key in o.hypothetical:
                owner = known.get(key) or seen.setdefault(key, native)
                if owner != native:
                    ident = f"{o.source.ident}~{native}"
                    source = SourceRef("telegram", ident)
                    mid = (o.raw_ref or "").split("/")[-1]
                    o = dataclasses.replace(
                        o,
                        source=source,
                        obs_id=make_obs_id("telegram", f"{ident}/{mid}", o.text),
                        raw_ref=f"{ident}/{mid}",
                        hypothetical=tuple(source.key if h == key else h for h in o.hypothetical),
                    )
            out.append(o)
        return out

    def parse(self, raw: Mapping[str, Any]) -> Iterable[Observation]:
        if not isinstance(raw, Mapping):
            raise FetcherError("enregistrement non objet")
        if raw.get("type") == "service":
            raise Skip  # création du canal, épinglage... : aucun texte à analyser
        channel = raw.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            raise FetcherError("canal manquant")
        if raw.get("_ident_exact") is not None:  # issu d'un export : déjà résolu par `unwrap`
            ident, src_exact = channel, bool(raw["_ident_exact"])
        elif channel.strip().startswith("@") or _BARE_ID.match(channel.strip()):
            ident, src_exact = (
                canonical_handle(channel),
                True,
            )  # enregistrement plat : un identifiant
        else:
            ident, src_exact = resolve_name(channel, self.names)
        if not ident:
            raise FetcherError("canal manquant")
        native_id = str(raw.get("message_id", raw.get("id", "")))
        text = _flatten(raw.get("text"))
        if not text and isinstance(raw.get("text_entities"), list):
            text = _flatten(raw["text_entities"])
        if not native_id and not text:
            raise FetcherError("ni identifiant ni texte")

        entities = raw.get("text_entities") or raw.get("entities")
        if not isinstance(entities, list):
            entities = raw["text"] if isinstance(raw.get("text"), list) else []
        exact_handles = list(extract_mentions(text))
        urls: list[str] = []
        for e in entities:
            if not isinstance(e, Mapping):
                continue
            kind = e.get("type")
            if kind == "mention" and isinstance(e.get("text"), str):
                exact_handles.append(e["text"])
            elif kind in ("link", "url"):
                url = e.get("url") or e.get("text")
                if isinstance(url, str) and url.strip():
                    urls.append(canonical_url(url))
            elif kind == "text_link":  # l'URL est dans `href` (à confirmer, Q16) ou `url`
                url = e.get("href") or e.get("url")
                if isinstance(url, str) and url.strip():
                    urls.append(canonical_url(url))
        mentions: dict[str, bool] = {}  # identifiant -> exact ?
        for h in exact_handles:
            if canonical_handle(h):
                mentions[canonical_handle(h)] = True
        fwd = raw.get("forwarded_from")
        if isinstance(fwd, str) and fwd.strip():
            fid, fexact = resolve_name(fwd, self.names)
            if fid:
                mentions[fid] = mentions.get(fid, False) or fexact

        source = SourceRef("telegram", ident)
        hypo = [source.key] if not src_exact else []
        hypo += [f"telegram:{i}" for i, exact in mentions.items() if not exact]
        meta: dict[str, str] = {}
        if isinstance(raw.get("_chat_name"), str) and raw["_chat_name"].strip():
            meta["nom"] = raw["_chat_name"].strip()
        if raw.get("_chat_id") is not None:
            meta["chat_id"] = str(raw["_chat_id"])
        yield Observation(
            obs_id=make_obs_id("telegram", f"{source.ident}/{native_id}", text),
            source=source,
            ts=parse_ts(raw.get("date_unixtime")) or parse_ts(raw.get("date")),
            text=text,
            mentions=dedup(SourceRef("telegram", i) for i in mentions),
            links=dedup(u for u in urls if u),
            hashtags=dedup(extract_hashtags(text)),
            engagement=to_int(raw.get("views")) + to_int(raw.get("reactions")),
            raw_ref=f"{source.ident}/{native_id}" if native_id else None,
            hypothetical=tuple(dict.fromkeys(hypo)),
            meta=meta,
        )
