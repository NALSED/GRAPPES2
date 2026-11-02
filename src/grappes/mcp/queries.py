"""Requêtes en lecture seule sur la base SQLite (logique du serveur MCP, testable sans MCP)."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from grappes.clustering.assign import level
from grappes.config import Config
from grappes.redact import pseudonym
from grappes.semantic.scoring import score_detail

MAX_LIMIT = 100
EXCERPT = 280


def open_readonly(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"base introuvable : {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def clamp(limit: int, default: int = 10) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, MAX_LIMIT))


def excerpt(text: str, n: int = EXCERPT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


class Queries:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg
        self.grappes = {g.grappe_id: g for g in cfg.grappes}

    # --- masquage des personnes (redact_persons, D30) --------------------------------------

    def _is_hidden(self, ref: str) -> bool:
        """Vrai si `ref` désigne une entité de type `person` et que le masquage est actif."""
        if not self.cfg.scoring.redact_persons:
            return False
        r = self.conn.execute("SELECT type FROM entities WHERE primary_ref=?", (ref,)).fetchone()
        return bool(r and r["type"] == "person")

    def _ref(self, ref: str) -> str:
        return pseudonym(ref) if self._is_hidden(ref) else ref

    def _name(self, ref: str, name: str | None) -> str | None:
        return "" if self._is_hidden(ref) else name

    # --- utilitaires ----------------------------------------------------------------------

    def run_id(self) -> str:
        r = self.conn.execute(
            "SELECT run_id FROM runs ORDER BY ts DESC, rowid DESC LIMIT 1"
        ).fetchone()
        if not r:
            raise ValueError("aucun run en base : lancer `grappes run` d'abord")
        return r["run_id"]

    def _grappe(self, grappe_id: str):
        if grappe_id not in self.grappes:
            raise ValueError(
                f"grappe inconnue : {grappe_id} (disponibles : {sorted(self.grappes)})"
            )
        return self.grappes[grappe_id]

    def _seuil(self) -> float:
        return self.cfg.scoring.seuil_min

    def _members(self, grappe_id: str, run_id: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT m.*, e.primary_ref, e.type, e.display_name FROM memberships m"
            " JOIN entities e USING(entity_id) WHERE m.grappe_id=? AND m.run_id=?"
            " AND m.confidence >= ?",
            (grappe_id, run_id, self._seuil()),
        ).fetchall()
        return {r["primary_ref"]: r for r in rows}

    def _weight(self, ref: str) -> float:
        r = self.conn.execute("SELECT weight FROM sources WHERE ref_key=?", (ref,)).fetchone()
        return r["weight"] if r else self.cfg.sources.weight(ref)

    # --- outils ---------------------------------------------------------------------------

    def list_grappes(self) -> list[dict]:
        run = self.run_id()
        out = []
        for gid, g in sorted(self.grappes.items()):
            n = len(self._members(gid, run))
            out.append({"grappe_id": gid, "label": g.label, "n_entities": n})
        return out

    def get_grappe(self, grappe_id: str) -> dict:
        g = self._grappe(grappe_id)
        run = self.run_id()
        members = self._members(grappe_id, run)
        levels = {"confirmé": 0, "probable": 0, "faible": 0}
        for m in members.values():
            # « confirmé » exige un recoupement validé ; sinon « probable » au mieux (FINAL.md § 7)
            levels[level(m["confidence"], self.cfg, m["via"] == "recoupement")] += 1
        n_valid = self.conn.execute(
            "SELECT COUNT(*) FROM validations WHERE grappe_id=? AND validated=1 AND run_id=?",
            (grappe_id, run),
        ).fetchone()[0]
        return {
            "grappe_id": g.grappe_id,
            "label": g.label,
            "anchors": list(g.anchors),
            "oppositions": list(g.oppositions),
            "n_lexicon_terms": len(g.lexicon.terms),
            "n_entities": len(members),
            "levels": levels,
            "n_validated_refs": n_valid,
            "run_id": run,
        }

    def top_sources(
        self, grappe_id: str, limit: int = 10, ecosystem: str | None = None
    ) -> list[dict]:
        """influence(s, g) = poids(s) × (1 + ln(1 + n_citants_distincts_membres(g))) × confiance / 100."""
        self._grappe(grappe_id)
        run = self.run_id()
        members = self._members(grappe_id, run)
        member_refs = set(members)
        citers: dict[str, set[str]] = {}
        for r in self.conn.execute(
            "SELECT DISTINCT citer, cited FROM citations WHERE run_id=?", (run,)
        ):
            if r["citer"] in member_refs and r["citer"] != r["cited"]:
                citers.setdefault(r["cited"], set()).add(r["citer"])
        rows = []
        for ref, m in members.items():
            eco = ref.split(":", 1)[0]
            if ecosystem and eco != ecosystem:
                continue
            n = len(citers.get(ref, ()))
            infl = self._weight(ref) * (1 + math.log(1 + n)) * m["confidence"] / 100
            rows.append(
                {
                    "entity_id": m["entity_id"],
                    "ref": self._ref(ref),
                    "display_name": self._name(ref, m["display_name"]),
                    "ecosystem": eco,
                    "confidence": m["confidence"],
                    "n_member_citers": n,
                    "weight": self._weight(ref),
                    "influence": round(infl, 3),
                }
            )
        rows.sort(key=lambda r: (-r["influence"], r["entity_id"]))
        return rows[: clamp(limit)]

    def search_entities(
        self, query: str, grappe_id: str | None = None, min_confidence: float = 0, limit: int = 20
    ) -> list[dict]:
        if grappe_id:
            self._grappe(grappe_id)
        run = self.run_id()
        q = (
            "%"
            + query.strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )
        sql = (
            "SELECT e.entity_id, e.type, e.primary_ref, e.display_name, m.grappe_id, m.confidence,"
            " m.status FROM entities e JOIN memberships m ON m.entity_id=e.entity_id AND m.run_id=?"
            " WHERE (lower(e.primary_ref) LIKE ? ESCAPE '\\' OR lower(e.display_name) LIKE ? ESCAPE '\\')"
            " AND m.confidence >= ?"
        )
        args: list = [run, q, q, float(min_confidence)]
        if grappe_id:
            sql += " AND m.grappe_id=?"
            args.append(grappe_id)
        sql += " ORDER BY m.confidence DESC, e.entity_id, m.grappe_id LIMIT ?"
        args.append(clamp(limit, 20))
        rows = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            d["display_name"] = self._name(d["primary_ref"], d["display_name"])
            d["primary_ref"] = self._ref(d["primary_ref"])
            rows.append(d)
        return rows

    def _entity(self, entity_id: str) -> sqlite3.Row:
        r = self.conn.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        if not r:
            raise ValueError(f"entité inconnue : {entity_id}")
        return r

    def get_entity(self, entity_id: str) -> dict:
        e = self._entity(entity_id)
        run = self.run_id()
        ms = self.conn.execute(
            "SELECT grappe_id, semantic, ecosystem, anchor, confidence, via, status FROM memberships"
            " WHERE entity_id=? AND run_id=? ORDER BY confidence DESC, grappe_id",
            (entity_id, run),
        ).fetchall()
        n_obs = self.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE source_key=?", (e["primary_ref"],)
        ).fetchone()[0]
        n_cit = self.conn.execute(
            "SELECT COUNT(DISTINCT citer) FROM citations WHERE cited=? AND run_id=?",
            (e["primary_ref"], run),
        ).fetchone()[0]
        return {
            "entity_id": e["entity_id"],
            "type": e["type"],
            "primary_ref": self._ref(e["primary_ref"]),
            "display_name": self._name(e["primary_ref"], e["display_name"]),
            "n_observations": n_obs,
            "n_citing_sources": n_cit,
            "memberships": [dict(m) for m in ms],
            "run_id": run,
        }

    def explain_assignment(self, entity_id: str, grappe_id: str) -> dict:
        g = self._grappe(grappe_id)
        e = self._entity(entity_id)
        run = self.run_id()
        ref = e["primary_ref"]
        m = self.conn.execute(
            "SELECT * FROM memberships WHERE entity_id=? AND grappe_id=? AND run_id=?",
            (entity_id, grappe_id, run),
        ).fetchone()
        own = self.conn.execute(
            "SELECT obs_id, text FROM observations WHERE source_key=? ORDER BY obs_id", (ref,)
        ).fetchall()
        context = "own"
        if not own:
            context = "citations"
            own = self.conn.execute(
                "SELECT DISTINCT o.obs_id, o.text FROM citations c JOIN observations o USING(obs_id)"
                " WHERE c.cited=? AND c.run_id=? ORDER BY o.obs_id",
                (ref, run),
            ).fetchall()
        hidden = self._is_hidden(ref)
        kw = {**g.lexicon.terms, **{k: -v for k, v in g.lexicon.negative.items()}}
        scored = sorted(
            ((score_detail(r["text"], kw, scale=g.scale), r) for r in own),
            key=lambda t: (-t[0].score, t[1]["obs_id"]),
        )[: self.cfg.scoring.top_n_observations]
        terms = [
            {
                "obs_id": r["obs_id"],
                "score": d.score,
                "matched": d.matched,
                "excerpt": "[masqué]" if hidden else excerpt(r["text"]),
            }
            for d, r in scored
        ]
        citers = [
            r["citer"]
            for r in self.conn.execute(
                "SELECT DISTINCT citer FROM citations WHERE cited=? AND run_id=? ORDER BY citer",
                (ref, run),
            )
        ]
        hypo = [
            self._ref(r["citer"])
            for r in self.conn.execute(
                "SELECT DISTINCT citer FROM citations WHERE cited=? AND run_id=? AND hypothesis=1"
                " ORDER BY citer",
                (ref, run),
            )
        ]
        v = self.conn.execute(
            "SELECT * FROM validations WHERE cited=? AND grappe_id=? AND run_id=?",
            (ref, grappe_id, run),
        ).fetchone()
        return {
            "entity_id": entity_id,
            "ref": self._ref(ref),
            "grappe_id": grappe_id,
            "membership": dict(m) if m else None,
            "semantic_context": context,
            "terms": terms,
            "citers": [self._ref(c) for c in citers],
            # citations dont l'identité est une hypothèse (D25) : signalées, jamais validantes
            "citations_hypothetiques": hypo,
            "validation": None
            if not v
            else {
                "validated": bool(v["validated"]),
                "n_citers": v["n_citers"],
                "n_ecosystems": v["n_ecosystems"],
                "weight_sum": v["weight_sum"],
                "reasons": json.loads(v["reasons"]),
                # E potentiel non retenu : recoupement refusé mais citants membres présents
                "recoupement_partiel": not v["validated"] and v["weight_sum"] > 0,
                "e_potentiel": round(
                    100.0
                    * min(1.0, v["weight_sum"] / (2 * self.cfg.scoring.crossref.min_weight_sum)),
                    2,
                ),
            },
            "is_anchor": ref in g.anchors,
            "run_id": run,
        }

    def get_validated_refs(self, grappe_id: str, limit: int = 20) -> list[dict]:
        self._grappe(grappe_id)
        run = self.run_id()
        rows = self.conn.execute(
            "SELECT * FROM validations WHERE grappe_id=? AND validated=1 AND run_id=?"
            " ORDER BY weight_sum DESC, n_citers DESC, cited LIMIT ?",
            (grappe_id, run, clamp(limit, 20)),
        ).fetchall()
        return [
            {
                "cited": self._ref(r["cited"]),
                "n_citers": r["n_citers"],
                "n_ecosystems": r["n_ecosystems"],
                "weight_sum": r["weight_sum"],
                "reasons": json.loads(r["reasons"]),
            }
            for r in rows
        ]

    def compare_grappes(self, a: str, b: str) -> dict:
        ga, gb = self._grappe(a), self._grappe(b)
        run = self.run_id()
        ma, mb = set(self._members(a, run)), set(self._members(b, run))
        both = ma & mb
        union = ma | mb
        return {
            "a": a,
            "b": b,
            "n_a": len(ma),
            "n_b": len(mb),
            "overlap": len(both),
            "jaccard": round(len(both) / len(union), 3) if union else 0.0,
            "shared_entities": [self._ref(r) for r in sorted(both)[:MAX_LIMIT]],
            "opposed": b in ga.oppositions or a in gb.oppositions,
            "oppositions_a": list(ga.oppositions),
            "oppositions_b": list(gb.oppositions),
        }
