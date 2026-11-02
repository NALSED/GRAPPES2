"""Registres officiels d'État -> fiabilité A (Admiralty) : enseignement supérieur (domaines), SIRENE/RNA (noms).

Un registre atteste l'existence et l'état actif d'un organisme, pas son appartenance à un sujet (DECISIONS.md D51) :
une correspondance place la SOURCE en fiabilité A, elle ne remplace jamais le recoupement entre écosystèmes.

Prudence : correspondance par nom = égalité exacte du nom normalisé, organisme actif, un seul résultat ; jamais pour
une personne ni pour un article Wikipédia. Réponses mises en cache en base (`cache_jours`), au plus
`max_requetes_par_cycle` requêtes par cycle, chaque appel journalisé. Sans cache, rien n'est supposé.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from grappes.canon import canonical_domain
from grappes.config import ConfigError
from grappes.search.paths import first, select
from grappes.search.safe import clean_url
from grappes.semantic.scoring import normalize
from grappes.storage.db import Database


@dataclass(frozen=True, slots=True)
class Registre:
    id: str
    nom: str
    methode: str  # « domaines » ou « nom »
    url: str
    params: dict[str, Any] = field(default_factory=dict)
    items: str = ""
    champ_url: str = ""
    champs_nom: tuple[str, ...] = ()
    etat_path: str = ""
    etat_valeur: str = ""
    identifiant: tuple[str, ...] = ()
    pagination: dict[str, Any] = field(default_factory=dict)
    champ_ville: str = (
        ""  # chemin de la commune dans la réponse (ville de l'organisme, jamais déduite)
    )
    pays: str = ""  # code ISO 2 lettres du pays du registre (source fiable du pays), ou vide


@dataclass(frozen=True, slots=True)
class RegistresConfig:
    registres: tuple[Registre, ...] = ()
    cache_jours: int = 30
    max_requetes: int = 30


def load_registres(path: Path) -> RegistresConfig:
    """`config/registres.yaml`. Absent : aucun registre (aucune source n'est placée en A)."""
    if not path.exists():
        return RegistresConfig()
    try:
        d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(d, dict) or not isinstance(d.get("registres", {}), dict):
        raise ConfigError(f"{path}: `registres` doit être un mapping")
    out: list[Registre] = []
    for rid, s in (d.get("registres") or {}).items():
        if not isinstance(s, dict) or s.get("methode") not in ("domaines", "nom"):
            raise ConfigError(
                f"{path}: registre {rid!r} : `methode` doit être « domaines » ou « nom »"
            )
        url = str(s.get("url", ""))
        if not url.startswith("https://"):
            raise ConfigError(f"{path}: registre {rid!r} : `url` https:// attendue")
        etat = s.get("etat") or {}
        out.append(
            Registre(
                id=str(rid),
                nom=str(s.get("nom", rid)),
                methode=s["methode"],
                url=url,
                params=dict(s.get("params") or {}),
                items=str(s.get("items", "")),
                champ_url=str(s.get("champ_url", "")),
                champs_nom=tuple(str(x) for x in s.get("champs_nom") or ()),
                etat_path=str(etat.get("path", "")),
                etat_valeur=str(etat.get("valeur", "")),
                identifiant=tuple(str(x) for x in s.get("identifiant") or ()),
                pagination=dict(s.get("pagination") or {}),
                champ_ville=str(s.get("champ_ville", "")),
                pays=str(s.get("pays", "")).upper(),
            )
        )
    return RegistresConfig(
        tuple(out), int(d.get("cache_jours", 30)), int(d.get("max_requetes_par_cycle", 30))
    )


def norm_nom(text: str) -> str:
    """Nom normalisé pour comparaison : casse, accents, ponctuation et espaces ignorés."""
    return re.sub(r"[^\w]+", "", normalize(text))


def _stale(maj: str | None, now: datetime, days: int) -> bool:
    return maj is None or datetime.fromisoformat(maj) < now - timedelta(days=days)


def _get(spec: Registre, params: dict[str, Any], transport, journal, label: str) -> Any | None:
    url = spec.url + "?" + urllib.parse.urlencode(params)
    try:
        resp = transport(
            "GET", url, {"User-Agent": "grappes-collecte/1 (collecte automatique ; respecte robots.txt)", "Accept": "application/json"}, None, 20.0, 2_000_000
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - registre indisponible : on ne suppose rien
        journal.log(f"registre:{spec.id}", label, f"GET {spec.url}", f"erreur {type(exc).__name__}")
        return None
    journal.log(f"registre:{spec.id}", label, f"GET {spec.url}", str(resp.status))
    if resp.status != 200:
        return None
    try:
        return json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def refresh_domaines(
    db: Database, spec: Registre, cfg: RegistresConfig, now: datetime, transport, journal
) -> int:
    """Charge en cache les domaines officiels (un registre par domaines est petit : quelques requêtes). Retourne
    le nombre de requêtes faites."""
    last = db.conn.execute(
        "SELECT MAX(maj) FROM registre_domaines WHERE registre=?", (spec.id,)
    ).fetchone()[0]
    if not _stale(last, now, cfg.cache_jours):
        return 0
    pas = int(spec.pagination.get("pas", 100))
    domains: dict[str, str] = {}  # domaine -> commune ('' si le registre n'en donne pas)
    n = 0
    for page in range(int(spec.pagination.get("max_pages", 1))):
        params = dict(spec.params)
        if spec.pagination.get("param"):
            params[spec.pagination["param"]] = page * pas
        data = _get(spec, params, transport, journal, "-")
        n += 1
        if data is None:
            break
        rows = select(data, spec.items)
        for row in rows:
            u = clean_url(first(row, spec.champ_url) or "")
            if u:
                d = canonical_domain(u)
                town = str(first(row, spec.champ_ville) or "") if spec.champ_ville else ""
                domains[d] = domains.get(d) or town
        if len(rows) < pas:
            break
    if domains:
        with db.transaction() as c:
            c.execute("DELETE FROM registre_domaines WHERE registre=?", (spec.id,))
            c.executemany(
                "INSERT INTO registre_domaines(registre, domaine, maj, ville) VALUES (?,?,?,?)",
                [(spec.id, d, now.isoformat(), domains[d]) for d in sorted(domains)],
            )
    return n


def lookup_nom(
    db: Database, spec: Registre, cfg: RegistresConfig, name: str, now: datetime, transport, journal
) -> int:
    """Interroge le registre pour un nom si le cache est périmé ou absent. Retourne le nombre de requêtes (0 ou 1)."""
    key = norm_nom(name)
    row = db.conn.execute(
        "SELECT maj FROM registre_noms WHERE registre=? AND nom=?", (spec.id, key)
    ).fetchone()
    if row is not None and not _stale(row["maj"], now, cfg.cache_jours):
        return 0
    params = {
        k: (str(v).replace("{nom}", name) if isinstance(v, str) else v)
        for k, v in spec.params.items()
    }
    data = _get(spec, params, transport, journal, name[:60])
    if data is None:
        return 1  # échec : rien n'est écrit, on ne suppose rien
    exact = []
    for item in select(data, spec.items):
        names = {norm_nom(str(first(item, p) or "")) for p in spec.champs_nom} - {""}
        if key in names:
            exact.append(item)
    actifs = [
        i for i in exact if not spec.etat_path or str(first(i, spec.etat_path)) == spec.etat_valeur
    ]
    trouve = (
        1 if len(actifs) == 1 and len(exact) == 1 else 0
    )  # homonymes ou organisme fermé : jamais de correspondance
    ident, ville = "", ""
    if trouve:
        if spec.champ_ville:
            ville = str(first(actifs[0], spec.champ_ville) or "")
        for p in spec.identifiant:
            v = first(actifs[0], p)
            if v:
                ident = str(v)
                break
    with db.transaction() as c:
        c.execute(
            "INSERT OR REPLACE INTO registre_noms(registre, nom, trouve, identifiant, maj, ville) VALUES (?,?,?,?,?,?)",
            (spec.id, key, trouve, ident, now.isoformat(), ville),
        )
    return 1


def update(db: Database, cfg: RegistresConfig, now: datetime, transport, journal) -> dict[str, int]:
    """Met à jour les caches (réseau !) : domaines officiels, puis noms des sites web nommés pas encore interrogés,
    dans la limite de `max_requetes` requêtes. Les personnes ne sont jamais interrogées."""
    stats = {"requetes": 0, "interroges": 0}
    left = cfg.max_requetes
    for spec in cfg.registres:
        if spec.methode == "domaines" and left > 0:
            used = refresh_domaines(db, spec, cfg, now, transport, journal)
            stats["requetes"] += used
            left -= used
    noms = [s for s in cfg.registres if s.methode == "nom"]
    if noms:
        rows = db.conn.execute(
            "SELECT ref_key, display_name FROM sources WHERE ecosystem='web' AND display_name IS NOT NULL ORDER BY ref_key"
        ).fetchall()
        for r in rows:
            for spec in noms:
                if left <= 0:
                    return stats
                used = lookup_nom(db, spec, cfg, r["display_name"], now, transport, journal)
                stats["requetes"] += used
                stats["interroges"] += used
                left -= used
    return stats


def matched_info(db: Database, cfg: RegistresConfig) -> dict[str, tuple[str, str]]:
    """Sources placées en A par les caches (aucun réseau) : domaine officiel, ou nom exact d'un organisme actif.
    Valeur : (commune, pays) fournis par le registre, chaînes vides si inconnus (jamais devinés)."""
    out: dict[str, tuple[str, str]] = {}
    if not cfg.registres:
        return out
    by_id = {r.id: r for r in cfg.registres}
    domains = {
        r["domaine"]: (r["ville"], by_id[r["registre"]].pays if r["registre"] in by_id else "")
        for r in db.conn.execute("SELECT registre, domaine, ville FROM registre_domaines")
    }
    found = {
        (r["registre"], r["nom"]): r["ville"]
        for r in db.conn.execute("SELECT registre, nom, ville FROM registre_noms WHERE trouve=1")
    }
    for r in db.conn.execute("SELECT ref_key, ecosystem, ident, display_name FROM sources"):
        if r["ecosystem"] in ("web", "rss"):
            host = canonical_domain(r["ident"])
            for d, info in domains.items():
                if host == d or host.endswith("." + d):
                    out[r["ref_key"]] = info
                    break
        if r["ecosystem"] == "web" and r["display_name"] and r["ref_key"] not in out:
            key = norm_nom(r["display_name"])
            for spec in cfg.registres:
                if spec.methode == "nom" and (spec.id, key) in found:
                    out[r["ref_key"]] = (found[spec.id, key], spec.pays)
                    break
    return out


def matched_refs(db: Database, cfg: RegistresConfig) -> frozenset[str]:
    """Sources placées en A par les caches (aucun réseau)."""
    return frozenset(matched_info(db, cfg))


def default_now() -> datetime:
    return datetime.now(UTC)
