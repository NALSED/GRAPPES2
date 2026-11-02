"""Cadre de données (D30) : retention_days = 90 et redact_persons actif par défaut."""

import csv
from datetime import UTC, datetime, timedelta

import pytest

from grappes.cli import main
from grappes.clustering.assign import assign
from grappes.clustering.export import export_csv
from grappes.config import ConfigError, load_config
from grappes.mcp.queries import Queries, open_readonly
from grappes.models import Observation, SourceRef
from grappes.pipeline import run
from grappes.redact import pseudonym
from grappes.storage.db import Database
from test_phase4 import STRONG, make_cfg, obs


def cfg_avec(tmp_path, **scoring):
    (tmp_path / "c").mkdir(exist_ok=True)
    return make_cfg(tmp_path / "c", **{"seuil_min": 30, **scoring})


def test_valeurs_par_defaut_du_depot():
    from conftest import ROOT

    sc = load_config(ROOT / "config").scoring
    assert sc.retention_days == 90 and sc.redact_persons is True
    assert sc.resultat_inclure_personnes is False


def test_valeurs_invalides(tmp_path):
    with pytest.raises(ConfigError):
        cfg_avec(tmp_path, retention_days=-3)


def test_retention_illimitee_si_null(tmp_path):
    assert cfg_avec(tmp_path, retention_days="null").scoring.retention_days is None


def test_entites_csv_masque_les_personnes(tmp_path):
    cfg = cfg_avec(tmp_path, redact_persons="true")
    o = [obs("x:jean_dupont", STRONG), obs("web:site.test", STRONG)]
    res = assign(o, cfg, names={"x:jean_dupont": "Jean Dupont", "web:site.test": "Le Site"})
    export_csv(res, cfg, "R-test", tmp_path / "o")
    rows = {
        r["primary_ref"]: r
        for r in csv.DictReader(
            (tmp_path / "o" / "entites.csv").open(encoding="utf-8"), delimiter=";"
        )
    }
    assert pseudonym("x:jean_dupont") in rows and "x:jean_dupont" not in rows
    p = rows[pseudonym("x:jean_dupont")]
    assert p["display_name"] == "" and p["entity_type"] == "person"
    assert rows["web:site.test"]["display_name"] == "Le Site"  # un site n'est pas masqué
    assert "Jean Dupont" not in (tmp_path / "o" / "entites.csv").read_text("utf-8")
    assert pseudonym("x:jean_dupont") == pseudonym("x:jean_dupont") != pseudonym("x:autre")


def test_entites_csv_en_clair_si_redact_desactive(tmp_path):
    cfg = cfg_avec(tmp_path, redact_persons="false")
    res = assign([obs("x:jean_dupont", STRONG)], cfg, names={"x:jean_dupont": "Jean Dupont"})
    export_csv(res, cfg, "R-test", tmp_path / "o")
    assert "Jean Dupont" in (tmp_path / "o" / "entites.csv").read_text("utf-8")


def _mcp(tmp_path, redact: str):
    cfg = cfg_avec(tmp_path, redact_persons=redact)
    db = Database(tmp_path / "m.db")
    db.add_observations(
        [
            Observation(
                "o1", SourceRef("x", "jean_dupont"), None, STRONG, meta={"nom": "Jean Dupont"}
            ),
            Observation("o2", SourceRef("web", "site.test"), None, STRONG, meta={"nom": "Le Site"}),
        ],
        {},
    )
    run(db, cfg)
    db.close()
    return Queries(open_readonly(tmp_path / "m.db"), cfg)


def test_mcp_masque_les_personnes(tmp_path):
    q = _mcp(tmp_path, "true")
    top = {r["ref"]: r for r in q.top_sources("G-A", 10)}
    assert pseudonym("x:jean_dupont") in top and "x:jean_dupont" not in top
    assert top[pseudonym("x:jean_dupont")]["display_name"] == "" and "web:site.test" in top
    found = q.search_entities("jean")  # la recherche ne retrouve pas une personne par son nom
    assert all(r["display_name"] == "" for r in found if r["type"] == "person")
    eid = next(r["entity_id"] for r in q.search_entities("") if r["type"] == "person")
    ent = q.get_entity(eid)
    assert ent["primary_ref"] == pseudonym("x:jean_dupont") and ent["display_name"] == ""
    fiche = q.explain_assignment(eid, "G-A")
    assert fiche["ref"] == pseudonym("x:jean_dupont")
    assert all(t["excerpt"] == "[masqué]" for t in fiche["terms"])
    assert "jean_dupont" not in str(fiche) and "Jean Dupont" not in str(fiche)


def test_mcp_en_clair_si_redact_desactive(tmp_path):
    q = _mcp(tmp_path, "false")
    assert "x:jean_dupont" in {r["ref"] for r in q.top_sources("G-A", 10)}


def test_purge_par_defaut_applique_retention_days(tmp_path, capsys):
    cfgdir = tmp_path / "c"
    cfg = cfg_avec(tmp_path, retention_days=90)
    db_path = tmp_path / "r.db"
    db = Database(db_path)
    now = datetime.now(UTC)
    db.add_observations(
        [
            Observation("vieille", SourceRef("x", "a"), now - timedelta(days=120), "t"),
            Observation("recente", SourceRef("x", "b"), now - timedelta(days=10), "t"),
            Observation("sans_date_ancienne", SourceRef("x", "c"), None, "t"),
            Observation("sans_date_recente", SourceRef("x", "d"), None, "t"),
        ],
        {},
    )
    # la date d'ingestion sert de repli : on vieillit l'une des observations sans date
    old = (now - timedelta(days=200)).isoformat()
    db.conn.execute(
        "UPDATE observations SET ingested_at=? WHERE obs_id='sans_date_ancienne'", (old,)
    )
    db.conn.commit()
    db.close()
    assert (
        main(["--db", str(db_path), "--config", str(cfgdir), "purge"]) == 0
    )  # défaut : retention_days
    assert "2 observations supprimées" in capsys.readouterr().out
    left = {o.obs_id for o in Database(db_path).load_observations()}
    assert left == {"recente", "sans_date_recente"}
    assert cfg.scoring.retention_days == 90


def test_purge_sans_duree_ni_retention_demande_une_duree(tmp_path, capsys):
    cfgdir = tmp_path / "c"
    cfg_avec(tmp_path, retention_days="null")
    code = main(["--db", str(tmp_path / "r.db"), "--config", str(cfgdir), "purge"])
    assert code == 2 and "--older-than" in capsys.readouterr().err


def test_ancienne_base_migree(tmp_path):
    import sqlite3

    p = tmp_path / "ancienne.db"
    conn = sqlite3.connect(p)
    conn.executescript(
        "CREATE TABLE observations (obs_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, ts TEXT,"
        " text TEXT NOT NULL, mentions TEXT NOT NULL DEFAULT '[]', links TEXT NOT NULL DEFAULT '[]',"
        " hashtags TEXT NOT NULL DEFAULT '[]', engagement INTEGER NOT NULL DEFAULT 0, raw_ref TEXT, run_id TEXT);"
        "INSERT INTO observations(obs_id, source_key, text) VALUES ('a','x:a','t');"
    )
    conn.commit()
    conn.close()
    db = Database(p)  # ajoute `ingested_at` sans perdre les données
    assert [o.obs_id for o in db.load_observations()] == ["a"]
