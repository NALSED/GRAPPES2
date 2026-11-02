"""Nettoyage des données reçues : tout résultat externe est non fiable.

On n'en extrait que des champs typés : texte sans caractères de contrôle et borné, URL http(s)
valide, date lisible. Rien n'est jamais exécuté ni interprété comme une instruction.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from grappes.canon import canonical_url
from grappes.ingestion.base import parse_ts

MAX_TEXT = 500
MAX_URL = 2048
MAX_EXTRA = 4000
_SPACES = re.compile(r"\s+")


def clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    """Texte brut d'un champ scalaire : sans caractères de contrôle, espaces réduits, borné."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    text = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch
        for ch in str(value)
        if unicodedata.category(ch) not in ("Cf", "Cs", "Co", "Cn")
    )
    text = _SPACES.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clean_url(value: Any) -> str:
    """URL `http(s)` avec un hôte, sans blanc ni contrôle, bornée ; sinon `""`."""
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if not url or len(url) > MAX_URL or re.search(r"[\s\x00-\x1f\x7f]", url):
        return ""
    if not re.match(r"https?://[^/?#\s]+", url, re.IGNORECASE):
        return ""
    return canonical_url(url)


def clean_date(value: Any) -> str:
    ts = (
        parse_ts(value)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool)
        else None
    )
    if ts is None and isinstance(
        value, str
    ):  # dates RSS (RFC 822) : « Tue, 02 Sep 2026 07:00:00 GMT »
        from email.utils import parsedate_to_datetime

        try:
            ts = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            ts = None
    return ts.date().isoformat() if ts else ""


def bounded_extra(item: Any) -> dict[str, Any]:
    """Champs spécifiques d'un fournisseur, conservés tels quels mais bornés (colonne `extra`)."""
    if not isinstance(item, Mapping):
        return {}
    try:
        if len(json.dumps(item, ensure_ascii=False, default=str)) <= MAX_EXTRA:
            return json.loads(json.dumps(item, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        pass
    return {str(k): clean_text(v, 200) for k, v in item.items() if not isinstance(v, (dict, list))}
