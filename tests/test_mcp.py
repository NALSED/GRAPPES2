import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from grappes.mcp.queries import MAX_LIMIT, Queries, clamp, excerpt, open_readonly
from grappes.pipeline import run
from grappes.storage.db import Database
from test_phase4 import chaine, make_cfg, obs

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {
    "list_grappes",
    "get_grappe",
    "top_sources",
    "search_entities",
    "get_entity",
    "explain_assignment",
    "get_validated_refs",
    "compare_grappes",
}


@pytest.fixture
def world(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg = make_cfg(cfg_dir)
    db_path = tmp_path / "g.db"
    db = Database(db_path)
    observations = chaine() + [obs("x:long", "soupe " * 200 + "é")]
    db.add_observations(observations, {})
    run(db, cfg)
    db.close()
    return db_path, cfg_dir, cfg


@pytest.fixture
def q(world):
    db_path, _, cfg = world
    return Queries(open_readonly(db_path), cfg)


def test_lecture_seule(world):
    db_path, _, _ = world
    conn = open_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM runs")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO runs VALUES ('a','b','c','d')")
    with pytest.raises(FileNotFoundError):
        open_readonly(db_path.parent / "absent.db")


def test_list_et_get_grappe(q):
    gs = {g["grappe_id"]: g for g in q.list_grappes()}
    assert set(gs) == {"G-A", "G-B"} and gs["G-A"]["n_entities"] >= 3
    d = q.get_grappe("G-A")
    assert d["anchors"] == ["x:anchor1", "x:anchor2"] and d["oppositions"] == ["G-B"]
    assert d["n_validated_refs"] >= 1
    with pytest.raises(ValueError):
        q.get_grappe("G-NOPE")


def test_top_sources_ordre_et_filtres(q):
    top = q.top_sources("G-A", limit=10)
    assert top and top == sorted(top, key=lambda r: (-r["influence"], r["entity_id"]))
    refs = [r["ref"] for r in top]
    assert "telegram:m" in refs  # validé par recoupement
    only_x = q.top_sources("G-A", ecosystem="x")
    assert only_x and all(r["ecosystem"] == "x" for r in only_x)
    # m est cité par 3 membres, donc plus influent qu'un membre jamais cité
    infl = {r["ref"]: r["influence"] for r in top}
    assert infl["telegram:m"] > infl["x:b"]


def test_limit_plafonne():
    assert clamp(10_000) == MAX_LIMIT and clamp(0) == 1 and clamp("zz") == 10 and clamp(-4) == 1


def test_search_entities(q):
    res = q.search_entities("x:a")
    assert res and all("x:a" in r["primary_ref"] or "a" in (r["display_name"] or "") for r in res)
    assert q.search_entities("%") == []  # le joker est échappé
    assert all(r["grappe_id"] == "G-A" for r in q.search_entities("x", grappe_id="G-A"))
    assert all(r["confidence"] >= 40 for r in q.search_entities("x", min_confidence=40))
    assert len(q.search_entities("x", limit=1)) == 1


def test_get_entity_et_explain(q):
    e = q.search_entities("telegram:m", grappe_id="G-A")[0]
    fiche = q.get_entity(e["entity_id"])
    assert fiche["primary_ref"] == "telegram:m" and fiche["memberships"][0]["grappe_id"] == "G-A"
    ex = q.explain_assignment(e["entity_id"], "G-A")
    assert ex["validation"]["validated"] and ex["membership"]["via"] == "recoupement"
    assert set(ex["citers"]) >= {"x:a", "x:b", "telegram:c"}
    assert all(len(t["excerpt"]) <= 280 for t in ex["terms"])
    with pytest.raises(ValueError):
        q.get_entity("E-inconnu")


def test_explain_texte_long_tronque_a_280(q):
    e = q.search_entities("x:long")[0]
    ex = q.explain_assignment(e["entity_id"], "G-A")
    assert ex["semantic_context"] == "own" and ex["terms"]
    assert all(len(t["excerpt"]) <= 280 for t in ex["terms"])
    assert excerpt("a" * 500).endswith("…") and len(excerpt("a" * 500)) == 280


def test_get_validated_refs(q):
    refs = q.get_validated_refs("G-A")
    assert {r["cited"] for r in refs} >= {"telegram:m", "x:n"}
    assert all(r["reasons"][0].startswith("validé") for r in refs)
    assert q.get_validated_refs("G-B") == []


def test_compare_grappes(q):
    c = q.compare_grappes("G-A", "G-B")
    assert c["opposed"] is True and c["n_a"] >= 3 and c["overlap"] <= min(c["n_a"], c["n_b"])
    assert 0 <= c["jaccard"] <= 1


def test_sans_run(tmp_path):
    (tmp_path / "c").mkdir()
    cfg = make_cfg(tmp_path / "c")
    Database(tmp_path / "vide.db").close()
    with pytest.raises(ValueError):
        Queries(open_readonly(tmp_path / "vide.db"), cfg).list_grappes()


def test_serveur_mcp_stdio_de_bout_en_bout(world):
    """Les 8 outils répondent via un vrai client MCP (processus stdio)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    db_path, cfg_dir, _ = world
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "grappes.mcp.server"],
        env={
            **os.environ,
            "GRAPPES_DB": str(db_path),
            "GRAPPES_CONFIG": str(cfg_dir),
            "PYTHONPATH": str(ROOT / "src"),
        },
    )

    async def scenario():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            assert tools == TOOLS
            top = await s.call_tool("top_sources", {"grappe_id": "G-A", "limit": 3})
            assert not top.is_error
            payload = json.dumps([c.model_dump() for c in top.content])
            assert "telegram:m" in payload or "x:a" in payload
            bad = await s.call_tool("get_grappe", {"grappe_id": "G-NOPE"})
            assert bad.is_error
            for name, args in [
                ("list_grappes", {}),
                ("get_grappe", {"grappe_id": "G-A"}),
                ("search_entities", {"query": "x"}),
                ("get_validated_refs", {"grappe_id": "G-A"}),
                ("compare_grappes", {"a": "G-A", "b": "G-B"}),
            ]:
                assert not (await s.call_tool(name, args)).is_error, name

    asyncio.run(asyncio.wait_for(scenario(), 60))
