"""Configuration malformée : toujours une ConfigError lisible, jamais un plantage."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from grappes.cli import main
from grappes.config import ConfigError, load_config
from grappes.models import Observation, SourceRef
from grappes.storage.db import Database

BASE = "id: G\nlexicon: {terms: {a: 1}}\n"


def charge(tmp_path: Path, grappe: str = BASE, scoring: str | None = None, sources=None):
    (tmp_path / "grappes").mkdir(exist_ok=True)
    (tmp_path / "grappes" / "g.yaml").write_text(grappe, encoding="utf-8")
    if scoring is not None:
        (tmp_path / "scoring.yaml").write_text(scoring, encoding="utf-8")
    if sources is not None:
        (tmp_path / "sources.yaml").write_text(sources, encoding="utf-8")
    return load_config(tmp_path)


@pytest.mark.parametrize(
    "grappe",
    [
        "id: G\nscale: abc\nlexicon: {terms: {a: 1}}\n",
        "id: G\nscale: .nan\nlexicon: {terms: {a: 1}}\n",
        "id: G\nlexicon: [a, b]\n",
        "id: G\nlexicon: {terms: {a: .nan}}\n",
        "id: G\nlexicon: {terms: {a: .inf}}\n",
        "id: G\nanchors: x:chef\nlexicon: {terms: {a: 1}}\n",  # scalaire au lieu d'une liste
        "id: G\nanchors: ['sans_deux_points']\nlexicon: {terms: {a: 1}}\n",
        "id: G\nanchors: ['mastodon:foo']\nlexicon: {terms: {a: 1}}\n",
        "id: G\nanchors: ['x:@']\nlexicon: {terms: {a: 1}}\n",
        "id: G\noppositions: autre\nlexicon: {terms: {a: 1}}\n",
        "id: [\n",
        "id: 12\nlexicon: {terms: {a: 1}}\n",
    ],
)
def test_grappe_invalide(tmp_path, grappe):
    with pytest.raises(ConfigError):
        charge(tmp_path, grappe)


@pytest.mark.parametrize(
    "scoring",
    [
        "csv: 3\n",
        "crossref: oups\n",
        "poids_complementaire: x\n",
        "poids_confiance: [1]\n",
        "entity_types: liste\n",
        "seuil_min: .nan\n",
        "crossref: {window_days: -3}\n",
        "crossref: {window_days: 0}\n",
        "crossref: {min_weight_sum: 0}\n",
        "crossref: {min_weight_sum: .inf}\n",
        "crossref_floor: .inf\n",
    ],
)
def test_scoring_invalide(tmp_path, scoring):
    with pytest.raises(ConfigError):
        charge(tmp_path, scoring=scoring)


def test_references_canonicalisees(tmp_path):
    (tmp_path / "grappes").mkdir()
    (tmp_path / "grappes" / "g2.yaml").write_text("id: G2\nlexicon: {terms: {b: 1}}\n")
    cfg = charge(
        tmp_path,
        "id: G\nlexicon: {terms: {a: 1}}\n"
        "anchors: ['X:Chef_Demo', ' telegram : @Canal_A ', 'web:WWW.Exemple.TEST', 'x:chef_demo']\n"
        "oppositions: ['G2', 'X:Rival']\n",
    )
    g = cfg.grappes[0]
    assert g.anchors == ("x:chef_demo", "telegram:canal_a", "web:exemple.test")  # sans doublon
    assert g.oppositions == ("G2", "x:rival")


def test_window_days_null_accepte(tmp_path):
    assert (
        charge(tmp_path, scoring="crossref: {window_days: null}\n").scoring.crossref.window_days
        is None
    )


def test_purge_negative_refusee(tmp_path, capsys):
    db = Database(tmp_path / "x.db")
    db.add_observations(
        [Observation(obs_id="a", source=SourceRef("x", "u"), ts=datetime.now(UTC), text="t")], {}
    )
    with pytest.raises(ValueError):
        db.purge_older_than(-5)
    assert len(db.load_observations()) == 1
    db.close()
    code = main(
        ["--db", str(tmp_path / "x.db"), "--config", "config", "purge", "--older-than", "-5"]
    )
    assert code == 2 and "positif" in capsys.readouterr().err
    assert len(Database(tmp_path / "x.db").load_observations()) == 1


def test_lexique_facultatif_et_sujets_deviennent_des_grappes(tmp_path):
    """FINAL.md § 3 : aucune exigence de lexique saisi par Sednal ; un sujet suffit."""
    (tmp_path / "sujets.yaml").write_text(
        'sujets: ["Jardins partagés et potagers collectifs en ville"]\n', "utf-8"
    )
    cfg = load_config(tmp_path)
    (g,) = cfg.grappes
    assert g.grappe_id == "sujet-jardins-partages-et-potagers-collectifs-en" and g.subject
    assert g.lexicon.terms == {}  # pas encore amorcé
    (tmp_path / "grappes").mkdir()
    (tmp_path / "grappes" / "x.yaml").write_text("id: G\nanchors: ['web:exemple.test']\n", "utf-8")
    assert {g.grappe_id for g in load_config(tmp_path).grappes} == {
        "G",
        "sujet-jardins-partages-et-potagers-collectifs-en",
    }


def test_ni_grappe_ni_sujet_refuse_avec_message_clair(tmp_path):
    with pytest.raises(ConfigError, match="au moins un sujet"):
        load_config(tmp_path)
