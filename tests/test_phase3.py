import json
import shutil
from pathlib import Path

import pytest

from conftest import CONFIG_CLAIR
from grappes.cli import main
from grappes.config import ConfigError, load_config
from grappes.pipeline import export, ingest, run
from grappes.storage.db import Database

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]
CONFIG = CONFIG_CLAIR


def _folder(tmp_path: Path, name: str, fixture: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    shutil.copy(FIX / fixture, d / fixture)
    return d


@pytest.fixture
def loaded(tmp_path):
    cfg = load_config(CONFIG)
    db = Database(tmp_path / "t.db")
    for name, fx, eco in [
        ("x", "x.jsonl", "x"),
        ("t", "telegram.jsonl", "telegram"),
        ("w", "web.jsonl", "web"),
    ]:
        ingest(db, _folder(tmp_path, name, fx), eco, cfg)
    yield db, cfg
    db.close()


def test_config_ok_et_hash_stable():
    a, b = load_config(CONFIG), load_config(CONFIG)
    assert a.config_hash == b.config_hash
    assert [g.grappe_id for g in a.grappes] == ["G-DEMO-A", "G-DEMO-B"]
    assert a.sources.weight("x:inconnu") == 1.0


def test_config_hash_change_si_fichier_change(tmp_path):
    shutil.copytree(CONFIG, tmp_path / "c")
    h1 = load_config(tmp_path / "c").config_hash
    (tmp_path / "c" / "scoring.yaml").write_text("seuil_min: 50\n", encoding="utf-8")
    assert load_config(tmp_path / "c").config_hash != h1


@pytest.mark.parametrize(
    "contenu",
    [
        "id: ''\nlexicon: {terms: {a: 1}}",
        "id: G\nlexicon: {terms: {a: -1}}",
        "id: G\nlexicon: {terms: {a: 1}}\nscale: 0",
        "id: G\nlexicon: {terms: {a: 1}}\noppositions: [G-NOPE]",
        "- une liste",
    ],
)
def test_config_invalide(tmp_path, contenu):
    (tmp_path / "grappes").mkdir()
    (tmp_path / "grappes" / "g.yaml").write_text(contenu, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_config_dossier_absent_ou_sans_grappe(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_ingestion_idempotente(loaded, tmp_path):
    db, cfg = loaded
    n = len(db.load_observations())
    new, _rep = ingest(db, tmp_path / "x", "x", cfg)
    assert new == 0 and len(db.load_observations()) == n


def test_roundtrip_observations(loaded):
    db, _ = loaded
    o = next(o for o in db.load_observations() if o.raw_ref == "1001")
    assert o.source.key == "x:chef_demo" and o.hashtags == ("cuisine",)
    assert o.mentions[0].key == "x:jardin_demo" and o.ts is not None


def test_affectation_semantique(loaded):
    db, cfg = loaded
    _, res = run(db, cfg)
    by_ref = {e.primary_ref: e.entity_id for e in res.entities}
    main = {}
    for m in res.memberships:
        if m["status"] != "secondaire":
            main[m["entity_id"]] = m
    assert main[by_ref["x:jardin_demo"]]["grappe_id"] == "G-DEMO-B"
    assert main[by_ref["telegram:canal_cuisine"]]["grappe_id"] == "G-DEMO-A"
    # entité sans texte propre : score calculé sur le contexte des citations
    assert by_ref["web:exemple-b.test"] in main
    assert all(0 <= m["confidence"] <= 100 for m in res.memberships)


def test_csv_rejouable_a_l_identique(loaded, tmp_path):
    db, cfg = loaded
    export(db, cfg, tmp_path / "o1")
    export(db, cfg, tmp_path / "o2")
    for name in ("entites.csv", "appartenances.csv"):
        assert (tmp_path / "o1" / name).read_bytes() == (tmp_path / "o2" / name).read_bytes()


def test_csv_format(loaded, tmp_path):
    db, cfg = loaded
    export(db, cfg, tmp_path / "o")
    lines = (tmp_path / "o" / "entites.csv").read_text("utf-8").splitlines()
    header = lines[0].split(";")
    assert header[0] == "entity_id" and header[-2:] == ["run_id", "admiralty"] and len(header) == 19
    assert all(len(line.split(";")) == 19 for line in lines)
    assert len(lines) == 1 + 8  # web:t.me remplacé par la citation du canal (D14)
    assert not (tmp_path / "o" / "entites.csv").read_bytes().startswith(b"\xef\xbb\xbf")


def test_db_etat_persistant_et_run_id_stable(loaded):
    db, cfg = loaded
    r1, _ = run(db, cfg)
    r2, _ = run(db, cfg)
    assert r1 == r2 and db.latest_run_id() == r1
    n = db.conn.execute("SELECT COUNT(*) FROM memberships WHERE run_id=?", (r1,)).fetchone()[0]
    assert n > 0


def test_purge(loaded):
    db, _ = loaded
    avant = len(db.load_observations())
    assert db.purge_older_than(1) > 0
    assert len(db.load_observations()) < avant


def test_cli_bout_en_bout(tmp_path, capsys):
    d = _folder(tmp_path, "in", "x.jsonl")
    db = str(tmp_path / "c.db")
    assert main(["--db", db, "--config", str(CONFIG), "ingest", str(d), "--ecosystem", "x"]) == 0
    assert main(["--db", db, "--config", str(CONFIG), "run"]) == 0
    assert main(["--db", db, "--config", str(CONFIG), "export", "--out", str(tmp_path / "s")]) == 0
    assert (tmp_path / "s" / "entites.csv").exists()
    assert main(["--db", db, "--config", str(tmp_path / "nope"), "run"]) == 2


def test_jsonl_ligne_cassee_comptee_en_rejet(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    (d / "a.jsonl").write_text(
        '{"id": 1, "author": "a", "text": "t"}\n{pas json\n', encoding="utf-8"
    )
    cfg = load_config(CONFIG)
    db = Database(":memory:")
    new, rep = ingest(db, d, "x", cfg)
    assert new == 1 and rep.n_rejected == 1
    json.dumps(rep.rejected)  # sérialisable


def test_cli_explain_purge_et_erreurs(tmp_path, capsys):
    d = _folder(tmp_path, "in", "x.jsonl")
    db = str(tmp_path / "c.db")
    base = ["--db", db, "--config", str(CONFIG)]
    assert main([*base, "ingest", str(tmp_path / "absent"), "--ecosystem", "x"]) == 2
    assert main([*base, "ingest", str(d), "--ecosystem", "x"]) == 0
    assert main([*base, "run"]) == 0
    capsys.readouterr()
    assert main([*base, "explain", "E-inconnu"]) == 1
    d2 = Database(db)
    eid = d2.conn.execute(
        "SELECT entity_id FROM entities WHERE primary_ref='x:chef_demo'"
    ).fetchone()[0]
    d2.close()
    capsys.readouterr()
    assert main([*base, "explain", eid]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["entite"]["primary_ref"] == "x:chef_demo" and "explication" in out
    assert main([*base, "purge", "--older-than", "1"]) == 0
    assert main(["--db", str(tmp_path / "absente.db"), "--config", str(CONFIG), "mcp"]) == 2


def test_charge_synthetique_petit(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("charge", ROOT / "outils" / "charge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = load_config(CONFIG)
    obs = list(mod.synth(500, 25))
    assert len(obs) == 500 and len({o.obs_id for o in obs}) == 500
    from grappes.clustering.assign import assign

    res = assign(obs, cfg)
    assert res.entities and res.iterations <= cfg.scoring.max_iter
