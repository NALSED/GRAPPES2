"""Classement Tranco (https://tranco-list.eu) : notoriété objective d'un domaine web, sans chiffre inventé.

Tranco est une liste académique ouverte et gratuite, conçue pour la recherche et résistante à la manipulation
(combinaison de plusieurs classements sur 30 jours). Elle est TÉLÉCHARGÉE UNE FOIS puis mise en cache (renouvelée
après `cache_jours`, jamais interrogée en boucle) ; l'identifiant et la date de la liste utilisée sont conservés dans
`meta.json` et repris dans le `run_id` pour la traçabilité. Le rang n'affine que le poids d'une source D (web
générique), entre D et C : une source D ne dépasse jamais le niveau de la lettre juste au-dessus.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from grappes.canon import canonical_domain

DEFAULT_API = "https://tranco-list.eu/api/lists/date/{date}"
MAX_BYTES = 60_000_000


def read_meta(cache_dir: str | Path) -> dict[str, Any] | None:
    p = Path(cache_dir) / "meta.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ensure(
    cache_dir: str | Path,
    now: datetime,
    transport,
    journal,
    max_age_days: int = 30,
    api: str = DEFAULT_API,
) -> dict[str, Any] | None:
    """Télécharge la liste Tranco si le cache manque ou a plus de `max_age_days` jours. Retourne les métadonnées
    (liste utilisée) ou `None` si rien n'est disponible. Un échec de téléchargement conserve l'ancien cache."""
    cache = Path(cache_dir)
    meta = read_meta(cache)
    if meta and (cache / "top-1m.csv").exists():
        try:
            if datetime.fromisoformat(meta["telecharge_le"]) > now - timedelta(days=max_age_days):
                return meta
        except (KeyError, ValueError):
            pass
    headers = {"User-Agent": "grappes-collecte/1 (collecte automatique ; respecte robots.txt)"}
    day = (
        (now.astimezone(UTC) - timedelta(days=1)).date().isoformat()
    )  # la liste du jour n'est pas toujours prête
    try:
        r = transport("GET", api.format(date=day), headers, None, 30.0, 1_000_000)
        journal.log("tranco", day, f"GET {api.format(date=day)}", str(r.status))
        info = json.loads(r.body.decode("utf-8")) if r.status == 200 else {}
        url = info.get("download")
        if not url or not info.get("available") or info.get("failed"):
            return meta
        d = transport("GET", url, headers, None, 120.0, MAX_BYTES)
        journal.log("tranco", day, f"GET {url}", str(d.status))
        if d.status != 200:
            return meta
        cache.mkdir(parents=True, exist_ok=True)
        tmp = cache / "top-1m.csv.tmp"
        tmp.write_bytes(d.body)
        tmp.replace(cache / "top-1m.csv")
        meta = {
            "liste_id": info.get("list_id", ""),
            "cree_le": info.get("created_on", ""),
            "telecharge_le": now.isoformat(),
            "url": url,
            "source": "https://tranco-list.eu",
        }
        (cache / "meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return meta
    except Exception as exc:  # noqa: BLE001 - réseau, JSON, disque : on garde l'ancien cache
        journal.log("tranco", day, "-", f"erreur {type(exc).__name__}")
        return meta


def candidates(host: str) -> list[str]:
    """Domaines à chercher pour un hôte : lui-même, puis ses parents (`a.b.fr` -> `b.fr`), sans le suffixe seul."""
    labels = canonical_domain(host).split(".")
    return [".".join(labels[i:]) for i in range(len(labels) - 1)] or [canonical_domain(host)]


def ranks_for(cache_dir: str | Path, hosts: set[str]) -> dict[str, int]:
    """Rang Tranco de chaque hôte (celui du domaine le plus proche présent dans la liste). Lecture en flux de la liste
    mise en cache : mémoire bornée, aucun réseau."""
    path = Path(cache_dir) / "top-1m.csv"
    if not hosts or not path.exists():
        return {}
    wanted: dict[str, list[str]] = {h: candidates(h) for h in hosts}
    need = {c for cs in wanted.values() for c in cs}
    found: dict[str, int] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if len(row) == 2 and row[1] in need and row[1] not in found:
                try:
                    found[row[1]] = int(row[0])
                except ValueError:
                    continue
    out: dict[str, int] = {}
    for host, cs in wanted.items():
        for c in cs:  # du plus précis au plus général
            if c in found:
                out[host] = found[c]
                break
    return out
