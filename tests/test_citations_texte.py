"""Citations par mention textuelle (DECISION_Q24_SUITE.md, D54) : entités déjà connues seulement."""

import dataclasses
from pathlib import Path

from grappes.clustering.assign import assign
from grappes.ecosystem.crossref import build_citations
from grappes.fiabilite import Reliability, load_fiabilite
from test_phase4 import STRONG, make_cfg, obs

TABLE = load_fiabilite(Path(__file__).resolve().parents[1] / "config" / "fiabilite_sources.yaml")
NAMES = {
    "web:lemonde.fr": "Le Monde",
    "web:cocarde.org": "Cocarde étudiante",
    "x:jean": "Jean Dupont",
}


def cited(o, names=NAMES):
    return {(c.cited, c.via) for c in build_citations([o], names)}


def test_nom_exact_d_une_entite_connue_cite_par_le_texte():
    o = obs("rss:letudiant.fr", "Selon Le Monde, la mobilisation étudiante s'étend.")
    assert cited(o) == {("web:lemonde.fr", "texte")}
    assert cited(obs("rss:x.fr", "voir lemonde.fr pour le détail")) == {("web:lemonde.fr", "texte")}


def test_nom_inconnu_ne_cree_aucune_entite():
    o = obs("rss:letudiant.fr", "Le journal Inconnu Quotidien et Madame Dupuis ont publié.")
    assert build_citations([o], NAMES) == []
    assert build_citations([o]) == []  # sans noms connus : comportement historique


def test_nom_partiellement_ressemblant_n_est_pas_confondu():
    for texte in (
        "Mondes parallèles",
        "Le Mondial 2026",
        "Cocardes étudiantes en tête",
        "le journal LeMonde.",
    ):
        assert cited(obs("rss:a.fr", texte)) == set(), texte
    assert cited(obs("rss:a.fr", "Le Monde diplomatique")) == {
        ("web:lemonde.fr", "texte")
    }  # nom exact en mots entiers


def test_ni_comptes_ni_personnes_ni_ecosysteme_non_textuel_ni_soi_meme():
    assert cited(obs("rss:a.fr", "Jean Dupont a déclaré")) == set()  # compte X : jamais par son nom
    assert (
        cited(obs("wikipedia:Article", "Le Monde écrit")) == set()
    )  # seuls rss et web citent par texte
    assert cited(obs("rss:lemonde.fr", "Le Monde écrit")) == set()  # son propre flux : écarté
    assert cited(obs("web:lemonde.fr", "Le Monde écrit")) == set()


def test_nom_ambigu_ou_trop_court_ecarte():
    names = {"web:a.org": "Union nationale", "web:b.org": "Union nationale", "web:c.org": "ENS"}
    assert build_citations([obs("rss:z.fr", "Union nationale et ENS")], names) == []


def test_mention_valide_un_recoupement_entre_ecosystemes(tmp_path):
    cfg = dataclasses.replace(make_cfg(tmp_path, seuil_min=40), fiabilite=TABLE)
    names = {"web:cible.org": "Cible Association"}
    base = [obs("web:cible.org", STRONG)]
    one_eco = [
        obs("rss:letudiant.fr", "La Cible Association agit. " + STRONG),
        obs("rss:liberation.fr", "Cible Association encore. " + STRONG),
        obs("rss:franceinfo.fr", "Cible Association toujours. " + STRONG),
    ]
    mixte = [
        obs("rss:letudiant.fr", "La Cible Association agit. " + STRONG),
        obs("rss:liberation.fr", "Cible Association encore. " + STRONG),
        obs("web:c.org", "Cible Association encore. " + STRONG),
        obs("web:c.org", "Cible Association toujours. " + STRONG),
    ]
    r1 = assign(base + one_eco, cfg, names, Reliability(TABLE))
    r2 = assign(base + mixte, cfg, names, Reliability(TABLE))
    v1 = [v for v in r1.validations if v.cited == "web:cible.org"]
    v2 = [v for v in r2.validations if v.cited == "web:cible.org"]
    assert not any(v.validated for v in v1)  # un seul écosystème : jamais validé
    assert any(v.validated and v.ecosystems == ("rss", "web") for v in v2)
    assert all(c.via == "texte" for c in r2.citations if c.cited == "web:cible.org")
    assert set(r2.names) >= {"web:cible.org"} and not any(
        "inconnu" in e.primary_ref for e in r2.entities
    )


def test_mention_pese_moins_qu_un_lien():
    r = Reliability(TABLE)
    assert r.text_factor("rss:a.fr") < r.factor("rss:a.fr")
    assert r.text_factor("web:lemonde.fr") == TABLE.factors["C"]  # B -> C
    assert (
        Reliability(TABLE, frozenset({"web:x.org"})).text_factor("web:x.org") == TABLE.factors["B"]
    )  # A -> B
