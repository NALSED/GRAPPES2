"""Parcours complet sur le corpus synthétique : fichiers -> ingestion -> recoupement -> assignation ->
export -> `resultat.csv` (FINAL.md § 14, étape 3). Trois grappes, quatre écosystèmes."""

import csv
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outils"))
import corpus_synthetique

from grappes.cli import main
from grappes.config import load_config
from grappes.mcp.queries import Queries, open_readonly
from grappes.pipeline import export, ingest, run
from grappes.redact import pseudonym
from grappes.storage.db import Database

ECOS = ("x", "telegram", "instagram", "web")
A, B, C = corpus_synthetique.A, corpus_synthetique.B, corpus_synthetique.C


@pytest.fixture(scope="module")
def monde(tmp_path_factory):
    out = tmp_path_factory.mktemp("corpus3")
    attendu = corpus_synthetique.write(out)
    cfg = load_config(out / "config")
    db = Database(out / "c.db")
    reports = {eco: ingest(db, out / eco, eco, cfg) for eco in ECOS}
    run_id, res = run(db, cfg)
    paths = export(db, cfg, out / "sorties")
    return {
        "out": out, "attendu": attendu, "cfg": cfg, "db": db, "reports": reports, "run_id": run_id,
        "res": res, "paths": {p.name: p for p in paths},
        "by_ref": {e.primary_ref: e for e in res.entities},
    }  # fmt: skip


def lire(path):
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # UTF-8 sans BOM
    return list(csv.reader(raw.decode("utf-8").splitlines()))


def membre(monde, ref, grappe=None):
    eid = monde["by_ref"][ref].entity_id
    ms = sorted(
        (m for m in monde["res"].memberships if m["entity_id"] == eid),
        key=lambda m: -m["confidence"],
    )
    return (
        next((m for m in ms if m["grappe_id"] == grappe), None)
        if grappe
        else (ms[0] if ms else None)
    )


# --- le corpus lui-même ---------------------------------------------------------------------


def test_corpus_reproductible_et_de_la_bonne_taille(monde, tmp_path):
    corpus_synthetique.write(tmp_path / "bis")
    h = lambda d: {
        str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(d.rglob("*")) if p.is_file() and p.suffix in (".json", ".jsonl", ".yaml")
    }  # fmt: skip
    assert h(monde["out"]) == h(tmp_path / "bis")  # mêmes fichiers à chaque génération
    assert 300 <= monde["attendu"]["n_observations_approx"] <= 900  # « quelques centaines »
    assert len(monde["cfg"].grappes) == 3


def test_ingestion_des_quatre_ecosystemes(monde):
    rep = monde["reports"]
    assert all(n > 0 and r.n_rejected == 0 for n, r in rep.values())
    assert rep["telegram"][1].ignored > 0  # messages de service comptés à part
    assert {e.primary_ref.split(":")[0] for e in monde["res"].entities} == set(ECOS)


# --- recoupements : validés, refusés ----------------------------------------------------------


@pytest.mark.parametrize(
    "g,dom", [(A, "cible-valide-a.test"), (B, "cible-valide-b.test"), (C, "cible-valide-c.test")]
)
def test_recoupements_valides(monde, g, dom):
    m = membre(monde, f"web:{dom}")
    assert (m["grappe_id"], m["via"], m["status"]) == (g, "recoupement", "classé")
    assert m["ecosystem"] > 0


@pytest.mark.parametrize(
    "dom,raison",
    [
        ("cible-peu-citee.test", "citants membres"),
        ("cible-mono-eco.test", "écosystèmes 1 < 2"),
        ("cible-bruit.test", "non membres"),
    ],
)
def test_recoupements_refuses_avec_raison(monde, dom, raison):
    rec = [r for r in monde["res"].validations if r.cited == f"web:{dom}" and r.grappe_id == A]
    if rec:  # un refus produit son enregistrement et sa raison
        assert not rec[0].validated and raison in " ".join(rec[0].reasons)
    m = membre(monde, f"web:{dom}")
    assert m is None or (m["status"] != "classé" and m["via"] != "recoupement")


def test_citations_hors_fenetre_ignorees(monde):
    rec = [
        r
        for r in monde["res"].validations
        if r.cited == "web:cible-hors-fenetre.test" and r.validated
    ]
    assert rec == []


def test_cercle_ferme_de_sources_neutres_non_classe(monde):
    for i in range(5):
        assert membre(monde, f"web:cercle-{i}.test") is None
    assert monde["res"].iterations <= monde["cfg"].scoring.max_iter


# --- ambiguïtés, oppositions, chevauchements ---------------------------------------------------


def test_opposition_et_marge_donnent_ambigu(monde):
    assert (
        membre(monde, "web:mixte-ab.test")["status"] == "ambigu"
    )  # opposition cuisine / jardinage
    assert membre(monde, "web:mixte-bc.test")["status"] == "ambigu"  # pas d'opposition : marge
    assert membre(monde, "web:marche-info.test")["status"] == "ambigu"  # terme commun à A et B
    assert (
        membre(monde, "web:marche-info.test", B) is not None
    )  # chevauchement : double appartenance


def test_source_sans_rapport_non_classee(monde):
    assert membre(monde, "web:sans-rapport.test") is None


# --- liens non identifiables -------------------------------------------------------------------


def test_liens_non_identifiables_ne_creent_aucune_entite(monde):
    refs = set(monde["by_ref"])
    assert not refs & {"web:t.me", "web:instagram.com", "web:x.com", "web:t.co"}
    assert any(c.cited.startswith("telegram:") for c in monde["res"].citations)  # t.me/<canal> cite


# --- canaux Telegram (D25) ---------------------------------------------------------------------


def test_canaux_exacts_hypothetiques_et_homonymes(monde):
    refs, att = set(monde["by_ref"]), monde["attendu"]["canaux"]
    assert set(att["homonymes"]) <= refs  # deux canaux « Canal Jardin » : jamais fusionnés
    assert monde["db"].native_ids()["telegram:canal_jardin~2002"] == "2002"
    assert set(att["exacts"]) <= refs
    assert any(c.hypothesis for c in monde["res"].citations)
    firm = {c.citer for c in monde["res"].citations if not c.hypothesis}
    assert "telegram:canal_cuisine_un" in firm  # table d'équivalence : citations fermes


# --- sortie finale -----------------------------------------------------------------------------


def test_resultat_csv_une_colonne_trie(monde):
    rows = lire(monde["paths"]["resultat.csv"])
    assert rows[0] == ["NOM"] and all(len(r) == 1 for r in rows)
    noms = [r[0] for r in rows[1:]]
    assert noms == sorted(noms, key=corpus_synthetique_sort)
    assert len(noms) == len(set(noms))


def corpus_synthetique_sort(n):
    from grappes.clustering.resultat import sort_key

    return sort_key(n)


def test_resultat_contenu_attendu_par_grappe(monde):
    att = monde["attendu"]
    for g, names in att["resultat"].items():
        got = [r[0] for r in lire(monde["paths"][f"resultat_{g}.csv"])[1:]]
        assert sorted(got, key=corpus_synthetique_sort) == sorted(
            set(names), key=corpus_synthetique_sort
        ), g
    tous = [r[0] for r in lire(monde["paths"]["resultat.csv"])[1:]]
    assert set(tous) == {n for names in att["resultat"].values() for n in names}
    assert not set(tous) & set(att["hors_resultat"])  # personnes, refus, ambigus, bruit : absents


def test_resultat_sans_identifiant_technique_ni_personne(monde):
    texte = monde["paths"]["resultat.csv"].read_text("utf-8")
    assert "://" not in texte and ".test" not in texte and "@" not in texte
    for personne in ("Alice", "Bob", "Clara", "Denis"):
        assert personne not in texte
    assert "insta_" not in texte  # Instagram : aucun nom lisible, donc rien à exporter


def test_entites_csv_masque_les_personnes(monde):
    path = monde["out"] / "sorties" / "entites.csv"
    rows = {r["primary_ref"]: r for r in csv.DictReader(path.open(encoding="utf-8"), delimiter=";")}
    assert pseudonym("x:alice_cuisine") in rows and "x:alice_cuisine" not in rows
    assert rows[pseudonym("x:alice_cuisine")]["display_name"] == ""
    assert (
        rows["x:journal_cuisine"]["display_name"] == "Journal Cuisine Demo"
    )  # compte institutionnel


def test_rejeu_a_l_identique(monde, tmp_path):
    cfg, db = monde["cfg"], monde["db"]
    assert run(db, cfg)[0] == monde["run_id"]
    export(db, cfg, tmp_path / "bis")
    for nom in monde["paths"]:
        assert (tmp_path / "bis" / nom).read_bytes() == monde["paths"][nom].read_bytes(), nom


# --- serveur MCP et CLI sur le même corpus -------------------------------------------------------


def test_top_sources_cuisine_via_mcp(monde):
    q = Queries(open_readonly(monde["out"] / "c.db"), monde["cfg"])
    top = q.top_sources(A, limit=10)
    assert len(top) == 10 and [r["influence"] for r in top] == sorted(
        (r["influence"] for r in top), reverse=True
    )
    assert not any("alice" in json.dumps(r).lower() for r in top)  # personnes masquées
    assert all(r["confidence"] >= monde["cfg"].scoring.seuil_min for r in top)


def test_parcours_complet_par_la_cli(monde, tmp_path, capsys):
    db, out = str(tmp_path / "cli.db"), tmp_path / "sorties"
    base = ["--db", db, "--config", str(monde["out"] / "config")]
    for eco in ECOS:
        assert main([*base, "ingest", str(monde["out"] / eco), "--ecosystem", eco]) == 0
    assert main([*base, "run"]) == 0
    assert main([*base, "export", "--out", str(out)]) == 0
    assert lire(out / "resultat.csv") == lire(monde["paths"]["resultat.csv"])
    assert sorted(p.name for p in out.glob("resultat*.csv")) == [
        "resultat.csv",
        f"resultat_{A}.csv",
        f"resultat_{B}.csv",
        f"resultat_{C}.csv",
        "resultat_tsites.csv",
    ]


def test_ingestion_incrementale_et_mauvais_ecosysteme(monde, tmp_path, capsys):
    cfg = monde["cfg"]
    db = Database(tmp_path / "inc.db")
    ingest(db, monde["out"] / "web", "web", cfg)
    r1, _ = run(db, cfg)
    assert ingest(db, monde["out"] / "x", "x", cfg)[0] > 0
    r2, _ = run(db, cfg)
    assert r1 != r2 and db.latest_run_id() == r2
    assert run(db, cfg)[0] == r2  # sans nouvelle donnée : même run_id
    code = main(
        [
            "--db",
            str(tmp_path / "w.db"),
            "--config",
            str(monde["out"] / "config"),
            "ingest",
            str(monde["out"] / "x"),
            "--ecosystem",
            "telegram",
        ]
    )
    assert code == 1 and "mauvais --ecosystem" in capsys.readouterr().err
