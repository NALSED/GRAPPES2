"""Cassette réseau : « collecter une fois, rejouer mille fois » (DECISION_AUTOCAL.md, quota).

Un transport d'enregistrement garde chaque réponse réseau (clé = méthode, URL, corps ; jamais les en-têtes,
donc jamais la clé d'API) dans une base SQLite par sujet. Le transport de rejeu ne parle jamais au réseau :
il sert la cassette, et une requête absente lève `SearchError` (la page compte alors comme « réseau », comme
une vraie panne). Même mécanisme que `rejouer --depuis` de trouver-sites, à la granularité de la requête :
toute la collecte (recherche, robots.txt, pages, registres) se rejoue, seuils d'ingestion compris.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from grappes.search.base import SearchError
from grappes.search.http import Response, urllib_transport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reponses (
    cle TEXT PRIMARY KEY, methode TEXT NOT NULL, url TEXT NOT NULL, statut INTEGER NOT NULL,
    en_tetes TEXT NOT NULL, corps BLOB NOT NULL, erreur TEXT NOT NULL DEFAULT ''
);
"""


def request_key(method: str, url: str, body: bytes | None) -> str:
    h = hashlib.sha256()
    for part in (method.upper().encode(), url.encode(), body or b""):
        h.update(len(part).to_bytes(8, "big"))
        h.update(part)
    return h.hexdigest()


class Cassette:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(_SCHEMA)

    def get(self, key: str) -> tuple[Response | None, str] | None:
        row = self.conn.execute(
            "SELECT statut, en_tetes, corps, erreur FROM reponses WHERE cle=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row[3]:
            return None, row[3]
        return Response(row[0], json.loads(row[1]), bytes(row[2])), ""

    def put(self, key: str, method: str, url: str, resp: Response | None, error: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO reponses VALUES (?,?,?,?,?,?,?)",
            (
                key,
                method.upper(),
                url,
                resp.status if resp else 0,
                json.dumps(dict(resp.headers) if resp else {}, ensure_ascii=False),
                resp.body if resp else b"",
                error,
            ),
        )
        self.conn.commit()

    def __len__(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM reponses").fetchone()[0]

    def urls(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT url FROM reponses ORDER BY url")]

    def close(self) -> None:
        self.conn.close()


class Recorder:
    """Transport d'enregistrement : appelle le réseau (ou `inner`) et garde la réponse. Une requête déjà
    en cassette est servie depuis elle (aucune consommation de quota en double)."""

    def __init__(self, cassette: Cassette, inner=urllib_transport):
        self.cassette, self.inner = cassette, inner
        self.network_calls = 0

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_bytes: int,
    ) -> Response:
        key = request_key(method, url, body)
        hit = self.cassette.get(key)
        if hit is not None:
            resp, err = hit
            if resp is None:
                raise SearchError(err)
            return resp
        self.network_calls += 1
        try:
            resp = self.inner(method, url, headers, body, timeout, max_bytes)
        except SearchError as exc:
            self.cassette.put(key, method, url, None, str(exc))
            raise
        self.cassette.put(key, method, url, resp)
        return resp


class Replayer:
    """Transport de rejeu : zéro réseau. Une requête absente de la cassette est une erreur franche."""

    def __init__(self, cassette: Cassette):
        self.cassette = cassette
        self.misses: list[str] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_bytes: int,
    ) -> Response:
        hit = self.cassette.get(request_key(method, url, body))
        if hit is None:
            self.misses.append(url)
            raise SearchError("cassette : requête absente (rejeu sans réseau)")
        resp, err = hit
        if resp is None:
            raise SearchError(err)
        return resp
