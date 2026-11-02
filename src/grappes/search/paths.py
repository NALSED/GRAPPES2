"""Chemins vers les champs d'une réponse (JSON, XML converti, CSV) et transformations simples.

Syntaxe : `a.b[0].c`, `a.*`, `liste[]` (tous les éléments), `lien.@href` (attribut XML),
`#text` (texte d'un élément XML portant des attributs). Aucun code n'est évalué.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

_PART = re.compile(r"([^\[\]]*)((?:\[[^\]]*\])*)$")
_INDEX = re.compile(r"\[([^\]]*)\]")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def _flatten(nodes: list[Any]) -> list[Any]:
    out: list[Any] = []
    for n in nodes:
        out.extend(n) if isinstance(n, list) else out.append(n)
    return out


def select(obj: Any, path: str | None) -> list[Any]:
    """Tous les nœuds désignés par `path` (liste vide si rien). `""` ou `None` : la racine."""
    if path in (None, ""):
        return _flatten([obj])
    nodes: list[Any] = [obj]
    for part in str(path).split("."):
        m = _PART.match(part)
        if not m:
            return []
        key, idxs = m.groups()
        nodes = _flatten(nodes)
        if key == "*":
            nodes = [v for n in nodes if isinstance(n, Mapping) for v in n.values()]
        elif key:
            nodes = [n[key] for n in nodes if isinstance(n, Mapping) and key in n]
        for idx in _INDEX.findall(idxs):
            picked: list[Any] = []
            for n in nodes:
                if not isinstance(n, list):
                    continue
                if idx in ("", "*"):
                    picked.extend(n)
                elif re.fullmatch(r"-?\d+", idx) and -len(n) <= int(idx) < len(n):
                    picked.append(n[int(idx)])
            nodes = picked
    return _flatten(nodes)


def first(obj: Any, path: str | None) -> Any:
    """Première valeur scalaire non vide désignée par `path`, sinon `None`."""
    for n in select(obj, path):
        if isinstance(n, Mapping) and "#text" in n:
            n = n["#text"]
        if isinstance(n, (str, int, float)) and not isinstance(n, bool) and str(n).strip():
            return n
    return None


def render_template(template: str, lookup: Callable[[str], Any]) -> str:
    """Remplace `{chemin}` par la valeur trouvée (chaîne vide si absente). Jamais de `format`."""
    return _PLACEHOLDER.sub(lambda m: "" if (v := lookup(m.group(1))) is None else str(v), template)


def resolve_field(item: Any, spec: Any) -> str:
    """Valeur d'un champ selon sa spécification.

    `spec` : un chemin, ou un mapping avec `path` | `template` (concaténation de chemins),
    `regex` (+ `group`), `prefix`, `suffix`, `default`.
    """
    if spec in (None, ""):
        return ""
    if isinstance(spec, str):
        spec = {"path": spec}
    if not isinstance(spec, Mapping):
        return ""
    if "template" in spec:
        value: Any = render_template(str(spec["template"]), lambda p: first(item, p)).strip()
    else:
        value = first(item, spec.get("path"))
    text = "" if value is None else str(value)
    if text and spec.get("regex"):
        m = re.search(str(spec["regex"]), text[:10_000])
        text = (m.group(int(spec.get("group", 1 if m and m.re.groups else 0))) or "") if m else ""
    if not text:
        return str(spec.get("default", ""))
    return f"{spec.get('prefix', '')}{text}{spec.get('suffix', '')}"
