"""Indépendance des voix : un éditeur ne compte qu'une fois dans un recoupement (D58)."""

import dataclasses
from pathlib import Path

from grappes.canon import site_key
from grappes.clustering.assign import assign
from grappes.fiabilite import Reliability, load_fiabilite
from test_phase4 import STRONG, make_cfg, obs

TABLE = load_fiabilite(Path(__file__).resolve().parents[1] / "config" / "fiabilite_sources.yaml")


def test_site_key():
    assert site_key("agency.lebonbon.fr") == site_key("lebonbon.fr") == "lebonbon.fr"
    assert (
        site_key("www1.rfi.fr") == "rfi.fr"
        and site_key("tempsreel.nouvelobs.com") == "nouvelobs.com"
    )
    assert site_key("news.bbc.co.uk") == "bbc.co.uk" and site_key("x.fr") == "x.fr"


def _validations(tmp_path, citers):
    cfg = dataclasses.replace(make_cfg(tmp_path, seuil_min=40), fiabilite=TABLE)
    o = [obs("web:cible.org", STRONG)]
    o += [obs(c, STRONG, links=["https://cible.org/page"]) for c in citers]
    res = assign(o, cfg, None, Reliability(TABLE))
    return next(v for v in res.validations if v.cited == "web:cible.org" and v.grappe_id == "G-A")


def test_flux_et_site_du_meme_editeur_ne_font_pas_deux_ecosystemes(tmp_path):
    v = _validations(tmp_path, ["rss:letudiant.fr", "web:letudiant.fr", "web:agency.letudiant.fr"])
    assert len(v.citers) == 1 and v.ecosystems in (("rss",), ("web",)) and not v.validated
    assert any("fusionné" in r for r in v.reasons)


def test_editeurs_distincts_restent_des_voix_distinctes(tmp_path):
    v = _validations(tmp_path, ["rss:letudiant.fr", "web:liberation.fr", "web:franceinfo.fr"])
    assert len(v.citers) == 3 and v.ecosystems == ("rss", "web") and v.validated
