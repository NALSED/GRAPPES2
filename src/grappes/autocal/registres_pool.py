"""Découverte par registres pour un sujet (chantier 1, D63) et fusion avec les candidats du web.

La phrase est traduite en requêtes de registre SANS lire la référence d'évaluation ; les résultats sont archivés
(`donnees/autocal/decouverte/<sujet>.json`) : un rejeu ne retourne jamais au réseau.

Fusion : une entité de registre dont le site officiel est le domaine d'un candidat du web enrichit ce candidat
(commune, registre) au lieu d'en créer un second ; deux registres qui nomment le même acteur (même SIREN, sinon même
nom normalisé) ne comptent qu'une fois. Les variantes de nom restantes (« Fenix Toulouse » / « Fenix Toulouse
Handball ») sont SIGNALÉES, jamais fusionnées d'office.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from grappes.classement.fusion import _groupes, fusionner, variantes
from grappes.decouverte.client import Http
from grappes.decouverte.registres import Decouvert, DecouverteConfig, decouvrir

DOSSIER = Path("donnees/autocal/decouverte")
CACHE_HTTP = Path("donnees/autocal/decouverte_cache")


def decouverts(sujet_id: str, phrase: str, cfg: DecouverteConfig | None = None, refaire: bool = False) -> list[Decouvert]:
    cfg = cfg or DecouverteConfig()
    f = DOSSIER / f"{sujet_id}.json"
    if f.exists() and not refaire:
        return [Decouvert(**d) for d in json.loads(f.read_text(encoding="utf-8"))]
    out = decouvrir(phrase, Http(CACHE_HTTP, delai_s=1.0), cfg)
    DOSSIER.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps([asdict(d) for d in out], ensure_ascii=False, indent=1), encoding="utf-8")
    return out



__all__ = ["CACHE_HTTP", "DOSSIER", "_groupes", "decouverts", "fusionner", "variantes"]
