"""Revue de robustesse : entrées vides, doublons, encodages, longs textes, dates, circularité."""

import csv
import json
import time

import pytest

from conftest import CONFIG_CLAIR
from grappes.config import load_config
from grappes.ingestion import get_fetcher
from grappes.pipeline import export, ingest, run
from grappes.semantic.scoring import score_proximity
from grappes.storage.db import Database

CONFIG = CONFIG_CLAIR


def rec(i, author, text, **extra):
    return json.dumps({"id": str(i), "author": author, "text": text, **extra}, ensure_ascii=False)


@pytest.fixture
def cfg():
    return load_config(CONFIG)


def pipeline(tmp_path, cfg, files, eco="x"):
    src = tmp_path / "in"
    src.mkdir()
    for name, content in files.items():
        (src / name).write_bytes(content if isinstance(content, bytes) else content.encode())
    db = Database(tmp_path / "t.db")
    new, report = ingest(db, src, eco, cfg)
    _, result = run(db, cfg)
    export(db, cfg, tmp_path / "out")
    return new, report, result


@pytest.mark.parametrize("eco", ["x", "telegram", "web"])
@pytest.mark.parametrize(
    "raw", [{}, {"text": None}, {"text": 123}, [], "chaine", None, {"author": None, "text": "a"}]
)
def test_enregistrements_degeneres_rejetes_sans_crash(eco, raw):
    obs, report = get_fetcher(eco).parse_many([raw])
    assert obs == [] and report.n_rejected == 1


def test_dossier_vide_et_json_vide(tmp_path, cfg):
    new, _, result = pipeline(tmp_path, cfg, {})
    assert new == 0 and result.entities == []
    assert (tmp_path / "out" / "entites.csv").read_text("utf-8").count("\n") == 1  # en-tête seul


def test_json_invalide_et_encodage_invalide_comptes_comme_rejets(tmp_path, cfg):
    files = {
        "a.json": "{oups",
        "b.jsonl": rec(1, "u", "café").encode("latin-1"),
        "c.json": "42",
        "d.jsonl": rec(2, "u", "soupe"),
    }
    new, report, _ = pipeline(tmp_path, cfg, files)
    assert new == 1 and report.n_rejected == 3


def test_bom_utf8_accepte(tmp_path, cfg):
    new, report, _ = pipeline(
        tmp_path, cfg, {"a.jsonl": b"\xef\xbb\xbf" + rec(1, "u", "soupe").encode()}
    )
    assert new == 1 and report.n_rejected == 0


def test_doublons_dans_le_lot_et_entre_lots(tmp_path, cfg):
    ligne = rec(1, "u", "soupe")
    new, report, _ = pipeline(tmp_path, cfg, {"a.jsonl": ligne + "\n" + ligne})
    assert new == 1 and report.duplicates == 1
    new2, _ = ingest(Database(tmp_path / "t.db"), tmp_path / "in", "x", cfg)
    assert new2 == 0  # ré-ingestion idempotente


def test_texte_vide_nul_non_chaine(tmp_path, cfg):
    lignes = [
        rec(1, "u", "soupe\u0000x"),
        rec(2, "u", ""),
        json.dumps({"id": "3", "author": "u", "text": None}),
        json.dumps({"id": "4", "author": "u", "text": 123}),
    ]
    new, report, result = pipeline(tmp_path, cfg, {"a.jsonl": "\n".join(lignes)})
    assert new + report.n_rejected == 4 and len(result.entities) == 1


def test_tres_long_texte_rapide():
    t0 = time.perf_counter()
    score = score_proximity("soupe " * 300_000 + "x" * 1_000_000, {"soupe": 1, "café crème": 1})
    assert 0 < score <= 100 and time.perf_counter() - t0 < 5


def test_caracteres_exotiques():
    assert score_proximity("\x00soupe\ud800 \U0001f600", {"soupe": 1}) > 0
    assert score_proximity("", {"soupe": 1}) == 0


def test_dates_absentes(tmp_path, cfg):
    lignes = [rec(1, "u1", "soupe recette @u2"), rec(2, "u2", "soupe", created_at="illisible")]
    _, _, result = pipeline(tmp_path, cfg, {"a.jsonl": "\n".join(lignes)})
    assert len(result.entities) == 2
    assert all(s.first_seen is None and s.last_seen is None for s in result.stats.values())
    rows = list(
        csv.reader((tmp_path / "out" / "entites.csv").open(encoding="utf-8"), delimiter=";")
    )
    assert all(r[-4] == "" and r[-3] == "" for r in rows[1:])


def test_citations_circulaires_bornees(tmp_path, cfg):
    lignes = [
        rec(i, f"u{i % 3}", f"soupe recette @u{(i + 1) % 3}", created_at="2026-03-01T00:00:00Z")
        for i in range(9)
    ]
    _, _, result = pipeline(tmp_path, cfg, {"a.jsonl": "\n".join(lignes)})
    assert len(result.entities) == 3 and result.iterations <= cfg.scoring.max_iter


def test_csv_neutralise_les_formules(tmp_path, cfg):
    lignes = [rec(1, "=cmd", "soupe"), rec(2, "+a;b", "soupe")]
    pipeline(tmp_path, cfg, {"a.jsonl": "\n".join(lignes)})
    rows = list(
        csv.reader((tmp_path / "out" / "entites.csv").open(encoding="utf-8"), delimiter=";")
    )
    for r in rows[1:]:
        assert r[2][:1] not in "=+-@" and r[3][:1] not in "=+-@"


# --- gros fichiers, lecture en flux, encodages (FINAL.md § 14, étape 4) --------------------------


def _ingest(tmp_path, files, eco="x", batch=5000, config=None):
    from grappes.pipeline import ingest

    src = tmp_path / "gros"
    src.mkdir()
    for name, content in files.items():
        (src / name).write_bytes(content if isinstance(content, bytes) else content.encode())
    cfg = load_config(config or CONFIG)
    db = Database(tmp_path / "g.db")
    new, rep = ingest(db, src, eco, cfg, batch=batch)
    return db, new, rep


def test_fichier_jsonl_volumineux_lu_en_flux_par_lots(tmp_path):
    import tracemalloc

    n = 60_000
    ligne = lambda i: rec(i, f"u{i % 50}", "soupe recette " + "x" * 200)
    contenu = "\n".join(ligne(i) for i in range(n)) + "\n"
    assert len(contenu) > 14_000_000  # plusieurs dizaines de Mo en mémoire si tout était chargé
    tracemalloc.start()
    _, new, rep = _ingest(tmp_path, {"a.jsonl": contenu}, batch=2000)
    _, pic = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert new == n and rep.accepted == n and rep.n_rejected == 0
    assert pic < 0.6 * len(contenu) * 3  # le pic reste très inférieur à « tout lu + tout parsé »


def test_doublons_entre_lots_comptes_et_non_reinseres(tmp_path):
    _, new, rep = _ingest(
        tmp_path, {"a.jsonl": "\n".join([rec(1, "u", "soupe")] * 7) + "\n"}, batch=2
    )
    assert new == 1 and rep.accepted == 1 and rep.duplicates == 6  # même en changeant de lot


def test_json_monolithique_trop_gros_refuse_mais_pas_le_jsonl(tmp_path):
    import shutil

    cfgdir = tmp_path / "cfg"
    shutil.copytree(CONFIG, cfgdir)
    p = cfgdir / "scoring.yaml"
    p.write_text(p.read_text("utf-8").replace("max_fichier_mo: 200", "max_fichier_mo: 1"), "utf-8")
    gros = json.dumps(
        [{"id": str(i), "author": "u", "text": "soupe " + "x" * 90} for i in range(20_000)]
    )
    assert len(gros) > 1_048_576
    _, new, rep = _ingest(
        tmp_path, {"a.json": gros, "b.jsonl": rec(1, "u", "soupe")}, config=cfgdir
    )
    assert new == 1 and rep.n_rejected == 1  # le .json trop gros est rejeté, le .jsonl passe


def test_ligne_geante_rejetee_sans_exploser(tmp_path):
    from grappes import pipeline

    geante = b'{"id": "1", "author": "u", "text": "' + b"a" * (pipeline.MAX_LINE + 10) + b'"}\n'
    _, new, rep = _ingest(tmp_path, {"a.jsonl": geante + rec(2, "u", "soupe").encode() + b"\n"})
    assert new == 1 and rep.n_rejected == 1


def test_octet_invalide_rejette_la_ligne_et_pas_le_fichier(tmp_path):
    bon = rec(1, "u", "soupe").encode()
    mauvais = b'{"id": "2", "author": "u", "text": "caf\xe9"}'  # latin-1 dans un fichier UTF-8
    _, new, rep = _ingest(
        tmp_path, {"a.jsonl": bon + b"\n" + mauvais + b"\n" + rec(3, "u", "recette").encode()}
    )
    assert new == 2 and rep.n_rejected == 1


def test_bom_seulement_en_tete_de_fichier(tmp_path):
    _, new, rep = _ingest(
        tmp_path,
        {
            "a.jsonl": b"\xef\xbb\xbf"
            + rec(1, "u", "soupe").encode()
            + b"\n"
            + rec(2, "u", "x").encode()
        },
    )
    assert new == 2 and rep.n_rejected == 0


def test_details_de_rejets_bornes_mais_compte_exact(tmp_path):
    from grappes.ingestion.base import RejectReport

    n = RejectReport.MAX_DETAILS + 500
    _, new, rep = _ingest(tmp_path, {"a.jsonl": "\n".join(["{pas du json"] * n) + "\n"}, batch=700)
    assert new == 0 and rep.n_rejected == n and len(rep.rejected) == RejectReport.MAX_DETAILS


def test_index_de_rejet_continus_entre_lots(tmp_path):
    lignes = [rec(i, "u", "soupe") if i % 3 else "{casse" for i in range(1, 10)]
    _, _, rep = _ingest(tmp_path, {"a.jsonl": "\n".join(lignes) + "\n"}, batch=2)
    assert [i for i, _ in rep.rejected] == [2, 5, 8]  # rangs absolus, pas relatifs au lot
