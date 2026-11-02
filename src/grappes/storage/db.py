"""Stockage SQLite."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from grappes import __version__
from grappes.models import Observation, SourceRef

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, ts TEXT NOT NULL, config_hash TEXT NOT NULL, code_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    ref_key TEXT PRIMARY KEY, ecosystem TEXT NOT NULL, ident TEXT NOT NULL,
    weight REAL NOT NULL, display_name TEXT, native_id TEXT, origin TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    obs_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, ts TEXT, text TEXT NOT NULL,
    mentions TEXT NOT NULL DEFAULT '[]', links TEXT NOT NULL DEFAULT '[]',
    hashtags TEXT NOT NULL DEFAULT '[]', engagement INTEGER NOT NULL DEFAULT 0,
    raw_ref TEXT, run_id TEXT, ingested_at TEXT, hypo TEXT NOT NULL DEFAULT '[]',
    fiabilite_admiralty TEXT
);
CREATE TABLE IF NOT EXISTS citations (
    citer TEXT NOT NULL, cited TEXT NOT NULL, ecosystem TEXT NOT NULL,
    obs_id TEXT NOT NULL, ts TEXT, run_id TEXT NOT NULL, hypothesis INTEGER NOT NULL DEFAULT 0,
    fiabilite_admiralty TEXT, credibilite_admiralty INTEGER, via TEXT NOT NULL DEFAULT 'lien'
);
CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY, type TEXT NOT NULL, primary_ref TEXT NOT NULL UNIQUE,
    display_name TEXT
);
CREATE TABLE IF NOT EXISTS memberships (
    entity_id TEXT NOT NULL, grappe_id TEXT NOT NULL, semantic REAL NOT NULL,
    ecosystem REAL NOT NULL, anchor REAL NOT NULL, confidence REAL NOT NULL,
    via TEXT NOT NULL, status TEXT NOT NULL, run_id TEXT NOT NULL,
    fiabilite_admiralty TEXT, credibilite_admiralty INTEGER,
    PRIMARY KEY (entity_id, grappe_id, run_id)
);
CREATE TABLE IF NOT EXISTS validations (
    cited TEXT NOT NULL, grappe_id TEXT NOT NULL, n_citers INTEGER NOT NULL,
    n_ecosystems INTEGER NOT NULL, weight_sum REAL NOT NULL, validated INTEGER NOT NULL,
    reasons TEXT NOT NULL, run_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registre_domaines (
    registre TEXT NOT NULL, domaine TEXT NOT NULL, maj TEXT NOT NULL, ville TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (registre, domaine)
);
CREATE TABLE IF NOT EXISTS registre_noms (
    registre TEXT NOT NULL, nom TEXT NOT NULL, trouve INTEGER NOT NULL, identifiant TEXT NOT NULL DEFAULT '',
    maj TEXT NOT NULL, ville TEXT NOT NULL DEFAULT '', PRIMARY KEY (registre, nom)
);
CREATE INDEX IF NOT EXISTS idx_obs_source ON observations(source_key);
CREATE INDEX IF NOT EXISTS idx_cit_cited ON citations(cited);
CREATE INDEX IF NOT EXISTS idx_mem_grappe ON memberships(grappe_id, run_id);
"""


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self._migrate()

    def _migrate(self) -> None:
        """Ajoute les colonnes apparues après la première version (bases existantes)."""
        wanted = {
            "observations": [
                ("ingested_at", "TEXT"),
                ("hypo", "TEXT NOT NULL DEFAULT '[]'"),
                ("fiabilite_admiralty", "TEXT"),
            ],
            "memberships": [("fiabilite_admiralty", "TEXT"), ("credibilite_admiralty", "INTEGER")],
            "sources": [("native_id", "TEXT"), ("origin", "TEXT")],
            "registre_domaines": [("ville", "TEXT NOT NULL DEFAULT ''")],
            "registre_noms": [("ville", "TEXT NOT NULL DEFAULT ''")],
            "citations": [
                ("hypothesis", "INTEGER NOT NULL DEFAULT 0"),
                ("fiabilite_admiralty", "TEXT"),
                ("credibilite_admiralty", "INTEGER"),
                ("via", "TEXT NOT NULL DEFAULT 'lien'"),
            ],
        }
        for table, columns in wanted.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    if table == "registre_domaines" and name == "ville":
                        # ancien cache sans commune : vidé une fois, il sera rechargé au prochain cycle
                        self.conn.execute("DELETE FROM registre_domaines")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    # --- observations ---------------------------------------------------------------------

    def add_observations(
        self,
        observations: Iterable[Observation],
        weights: dict[str, float],
        letter: Callable[[str], str] | None = None,
    ):
        """Insère (idempotent sur obs_id). Retourne le nombre de nouvelles observations.
        `letter` : fiabilité Admiralty (A à F) d'une source, inscrite dans l'observation à l'ingestion."""
        new = 0
        with self.transaction() as c:
            for o in observations:
                c.execute(
                    "INSERT OR IGNORE INTO sources(ref_key, ecosystem, ident, weight)"
                    " VALUES (?,?,?,?)",
                    (
                        o.source.key,
                        o.source.ecosystem,
                        o.source.ident,
                        weights.get(o.source.key, 1.0),
                    ),
                )
                cur = c.execute(
                    "INSERT OR IGNORE INTO observations(obs_id, source_key, ts, text, mentions,"
                    " links, hashtags, engagement, raw_ref, ingested_at, hypo, fiabilite_admiralty)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        o.obs_id,
                        o.source.key,
                        _iso(o.ts),
                        o.text,
                        json.dumps([m.key for m in o.mentions]),
                        json.dumps(list(o.links)),
                        json.dumps(list(o.hashtags)),
                        o.engagement,
                        o.raw_ref,
                        datetime.now(UTC).isoformat(),
                        json.dumps(list(o.hypothetical)),
                        letter(o.source.key) if letter else None,
                    ),
                )
                new += cur.rowcount
                native = o.meta.get("chat_id")
                if native:  # identifiant natif du canal (détection des homonymes, D25)
                    c.execute(
                        "UPDATE sources SET native_id=? WHERE ref_key=? AND native_id IS NULL",
                        (native, o.source.key),
                    )
                name = o.meta.get("nom")
                if name:  # nom lisible de la source (premier connu conservé)
                    c.execute(
                        "UPDATE sources SET display_name=? WHERE ref_key=? AND display_name IS NULL",
                        (name, o.source.key),
                    )
        return new

    # « recherche » > « exploration » > « fichier » : la provenance la plus fiable est conservée
    _ORIGIN_RANK: ClassVar[dict[str, int]] = {"recherche": 3, "exploration": 2, "fichier": 1}

    def set_origin(self, ref_key: str, origin: str) -> None:
        """Provenance d'une source : trouvée par la recherche, par exploration de liens, ou lue dans
        un fichier (défaut). Sert à décider quelles sources peuvent enseigner des mots (phase 9)."""
        row = self.conn.execute("SELECT origin FROM sources WHERE ref_key=?", (ref_key,)).fetchone()
        if row is None:
            return
        if self._ORIGIN_RANK.get(origin, 0) > self._ORIGIN_RANK.get(row["origin"] or "fichier", 1):
            self.conn.execute("UPDATE sources SET origin=? WHERE ref_key=?", (origin, ref_key))
            self.conn.commit()

    def origins(self) -> dict[str, str]:
        return {
            r["ref_key"]: r["origin"] or "fichier"
            for r in self.conn.execute("SELECT ref_key, origin FROM sources")
        }

    def native_ids(self) -> dict[str, str]:
        """Identifiants natifs connus des sources : `ref_key` -> identifiant (canaux Telegram)."""
        return {
            r["ref_key"]: r["native_id"]
            for r in self.conn.execute("SELECT ref_key, native_id FROM sources")
            if r["native_id"]
        }

    def load_display_names(self) -> dict[str, str]:
        """Noms lisibles connus des sources : `ref_key` -> nom."""
        return {
            r["ref_key"]: r["display_name"]
            for r in self.conn.execute("SELECT ref_key, display_name FROM sources")
            if r["display_name"]
        }

    def load_observations(self) -> list[Observation]:
        rows = self.conn.execute("SELECT * FROM observations ORDER BY obs_id").fetchall()
        out = []
        for r in rows:
            eco, _, ident = r["source_key"].partition(":")
            out.append(
                Observation(
                    obs_id=r["obs_id"],
                    source=SourceRef(eco, ident),
                    ts=_parse(r["ts"]),
                    text=r["text"],
                    mentions=tuple(SourceRef(*m.split(":", 1)) for m in json.loads(r["mentions"])),
                    links=tuple(json.loads(r["links"])),
                    hashtags=tuple(json.loads(r["hashtags"])),
                    engagement=r["engagement"],
                    raw_ref=r["raw_ref"],
                    hypothetical=tuple(json.loads(r["hypo"])),
                )
            )
        return out

    def purge_older_than(self, days: int, now: datetime | None = None) -> int:
        if days < 0:
            raise ValueError("la durée de purge doit être positive ou nulle")
        cutoff = ((now or datetime.now(UTC)).timestamp()) - days * 86400
        with self.transaction() as c:
            # date de l'observation, à défaut date d'ingestion : rien n'échappe à la rétention
            rows = c.execute(
                "SELECT obs_id, COALESCE(ts, ingested_at) AS d FROM observations"
                " WHERE COALESCE(ts, ingested_at) IS NOT NULL"
            ).fetchall()
            old = [r["obs_id"] for r in rows if datetime.fromisoformat(r["d"]).timestamp() < cutoff]
            c.executemany("DELETE FROM observations WHERE obs_id=?", [(i,) for i in old])
        return len(old)

    # --- runs -----------------------------------------------------------------------------

    def save_run(
        self, run_id: str, config_hash: str, entities, memberships, citations, validations=()
    ) -> None:
        """Remplace le contenu d'un run (rejouable)."""
        with self.transaction() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                (run_id, datetime.now(UTC).isoformat(), config_hash, __version__),
            )
            c.execute("DELETE FROM memberships WHERE run_id=?", (run_id,))
            c.execute("DELETE FROM citations WHERE run_id=?", (run_id,))
            c.execute("DELETE FROM validations WHERE run_id=?", (run_id,))
            c.executemany(
                "INSERT OR REPLACE INTO entities VALUES (?,?,?,?)",
                [(e.entity_id, e.type.value, e.primary_ref, e.display_name) for e in entities],
            )
            c.executemany(
                "INSERT INTO memberships(entity_id, grappe_id, semantic, ecosystem, anchor, confidence,"
                " via, status, run_id, fiabilite_admiralty, credibilite_admiralty)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        m["entity_id"],
                        m["grappe_id"],
                        m["semantic"],
                        m["ecosystem"],
                        m["anchor"],
                        m["confidence"],
                        m["via"],
                        m["status"],
                        run_id,
                        m.get("fiabilite"),
                        m.get("credibilite"),
                    )
                    for m in memberships
                ],
            )
            c.executemany(
                "INSERT INTO validations VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        v.cited,
                        v.grappe_id,
                        len(v.citers),
                        len(v.ecosystems),
                        v.weight_sum,
                        int(v.validated),
                        json.dumps(list(v.reasons), ensure_ascii=False),
                        run_id,
                    )
                    for v in validations
                ],
            )
            c.executemany(
                "INSERT INTO citations(citer, cited, ecosystem, obs_id, ts, run_id, hypothesis,"
                " fiabilite_admiralty, credibilite_admiralty, via) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        ci.citer,
                        ci.cited,
                        ci.ecosystem,
                        ci.obs_id,
                        _iso(ci.ts),
                        run_id,
                        int(ci.hypothesis),
                        ci.reliability or None,
                        ci.credibility,
                        ci.via,
                    )
                    for ci in citations
                ],
            )

    def latest_run_id(self) -> str | None:
        r = self.conn.execute(
            "SELECT run_id FROM runs ORDER BY ts DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return r["run_id"] if r else None
