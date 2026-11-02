"""Décodage des réponses : JSON, XML (RSS, Atom...) et CSV, avec des garde-fous."""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from typing import Any

from grappes.search.base import SearchError

MAX_ROWS = 10_000
_DOCTYPE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _strip_ns(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _xml_to_obj(el: ET.Element) -> Any:
    attrs = {f"@{_strip_ns(k)}": v for k, v in el.attrib.items()}
    text = (el.text or "").strip()
    children = list(el)
    if not children and not attrs:
        return text
    out: dict[str, Any] = dict(attrs)
    if text:
        out["#text"] = text
    for ch in children:
        tag, val = _strip_ns(ch.tag), _xml_to_obj(ch)
        if tag in out:
            if not isinstance(out[tag], list):
                out[tag] = [out[tag]]
            out[tag].append(val)
        else:
            out[tag] = val
    return out


def decode(body: bytes, fmt: str, *, delimiter: str = ",") -> Any:
    """Corps de réponse -> structure JSON-like. Erreur de forme : `SearchError`."""
    try:
        if fmt == "json":
            return json.loads(body.decode("utf-8-sig"))
        if fmt == "xml":
            if _DOCTYPE.search(body):  # pas de DTD ni d'entités : bombes d'expansion exclues
                raise SearchError("XML refusé : DOCTYPE/ENTITY interdits")
            root = ET.fromstring(body)
            return {_strip_ns(root.tag): _xml_to_obj(root)}
        if fmt == "csv":
            reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")), delimiter=delimiter)
            rows = []
            for row in reader:
                rows.append({str(k): v for k, v in row.items() if k is not None})
                if len(rows) >= MAX_ROWS:
                    break
            return rows
    except SearchError:
        raise
    except (ValueError, ET.ParseError, csv.Error, RecursionError, UnicodeDecodeError) as exc:
        raise SearchError(f"réponse {fmt} mal formée : {type(exc).__name__}") from exc
    raise SearchError(f"format de réponse inconnu : {fmt!r} (json, xml, csv)")
