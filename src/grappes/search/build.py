"""Construction d'un `Candidate` nettoyé à partir d'un élément brut de réponse."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from grappes.canon import canonical_handle, link_target
from grappes.search.models import Candidate
from grappes.search.paths import resolve_field, select
from grappes.search.safe import bounded_extra, clean_date, clean_text, clean_url

_ECO = re.compile(r"[a-z][a-z0-9_]{1,19}")
_RESERVED = {
    "x",
    "telegram",
    "instagram",
    "web",
}  # écosystèmes des lecteurs de fichiers et du récupérateur de pages
MAX_DOC_LINKS = 200


def _clean_ident(value: str) -> str:
    """Identifiant de source : minuscules, sans espace, 120 caractères au plus."""
    return re.sub(r"[^\w./~-]+", "_", clean_text(value, 120).casefold()).strip("_")


def _doc(item: Any, spec: Mapping[str, Any]) -> dict[str, Any] | None:
    """Document d'un résultat qui EST le contenu (`observation:` dans le YAML du fournisseur).

    `ecosysteme` (étiquette propre au fournisseur, distincte de « web »), `source` (identifiant de la source),
    `nom` (nom lisible), `liens` (chemin vers une liste d'URL citées), `exclure` (règles : `path`+`regex`, ou
    `absent: path` = en cas de doute on n'ingère pas). Tout est nettoyé : c'est une donnée non fiable.
    """
    eco = str(spec.get("ecosysteme", "")).strip()
    if not _ECO.fullmatch(eco) or eco in _RESERVED:
        return None
    source = _clean_ident(resolve_field(item, spec.get("source")))
    if not source:
        return None
    links: list[str] = []
    for v in select(item, spec.get("liens")) if spec.get("liens") else []:
        u = clean_url(v.get("url") if isinstance(v, Mapping) else v)
        if u and u not in links and len(links) < MAX_DOC_LINKS:
            links.append(u)
    excluded = ""
    for rule in spec.get("exclure") or []:
        if not isinstance(rule, Mapping):
            continue
        if "absent" in rule:
            hit = not [v for v in select(item, rule["absent"]) if v not in (None, "", [])]
            motif = rule.get("motif") or f"« {rule['absent']} » absent : en cas de doute, ignoré"
        else:
            rx = re.compile(str(rule.get("regex", "(?!)")))
            values = [
                str(v) for v in select(item, rule.get("path")) if isinstance(v, (str, int, float))
            ]
            hit = any(rx.search(v[:5000]) for v in values)
            motif = rule.get("motif") or "règle d'exclusion"
        if hit:
            excluded = clean_text(motif, 120)
            break
    return {
        "ecosysteme": eco,
        "source": source,
        "nom": clean_text(resolve_field(item, spec.get("nom")), 100),
        "liens": links,
        "exclu": excluded,
    }


def build_candidate(
    provider: str, keyword: str, item: Any, fields: Mapping[str, Any], options: Mapping[str, Any]
) -> Candidate | None:
    """`None` si l'élément ne permet d'identifier aucun candidat (ni URL, ni identifiant)."""
    if not isinstance(item, (Mapping, str)):
        return None
    if isinstance(item, str):  # élément scalaire : l'URL elle-même
        item = {"valeur": item}
        fields = {"url": "valeur", "title": "valeur"}
    url = clean_url(resolve_field(item, fields.get("url")))
    title = clean_text(resolve_field(item, fields.get("title")))
    description = clean_text(resolve_field(item, fields.get("description")))
    native = clean_text(resolve_field(item, fields.get("id")), 200)
    key = link_target(url) if url else ""
    if key:
        ecosystem, ident = key.split(":", 1)
    elif url:
        ecosystem, ident = "inconnu", url  # cible non identifiable (invitation, publication...)
    elif native:
        ecosystem = clean_text(options.get("ecosystem"), 40) or "inconnu"
        ident = canonical_handle(native) or native
    else:
        return None
    reason = clean_text(
        str(options.get("raison") or "résultat de recherche pour « {mot_cle} »")
        .replace("{mot_cle}", keyword)
        .replace("{fournisseur}", provider),
        200,
    )
    text = ". ".join(t for t in (title, description) if t)
    obs = options.get("observation")
    doc = _doc(item, obs) if isinstance(obs, Mapping) else None
    if doc is not None:
        ecosystem, ident = doc["ecosysteme"], doc["source"]
    return Candidate(
        doc=doc,
        ecosystem=ecosystem,
        ident=ident,
        url=url,
        keyword=keyword,
        provider=provider,
        reason=reason,
        date=clean_date(resolve_field(item, fields.get("date"))),
        text=text,
        extra=bounded_extra(item),
    )
