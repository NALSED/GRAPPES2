"""Niveau « confirmé » : réservé aux entités recoupées ; sans recoupement, « probable » au mieux."""

import csv

from grappes.clustering.assign import assign, level
from grappes.clustering.export import export_csv
from test_phase4 import STRONG, make_cfg, obs


def _cfg(tmp_path):
    (tmp_path / "c").mkdir(exist_ok=True)
    return make_cfg(tmp_path / "c", seuil_min=30, redact_persons="false")


def test_fonction_level(tmp_path):
    cfg = _cfg(tmp_path)
    assert level(95, cfg, validated=True) == "confirmé"
    assert level(95, cfg, validated=False) == "probable"  # trop sûr de lui sans recoupement
    assert (
        level(50, cfg, validated=False) == "probable"
        and level(10, cfg, validated=False) == "faible"
    )


def test_export_et_recoupement(tmp_path):
    cfg = _cfg(tmp_path)
    cible = "https://cible.test/p"
    o = [obs("web:cible.test", "soupe")]
    for i in range(2):
        o += [obs(f"x:m{i}", STRONG), obs(f"x:m{i}", "neutre", links=[cible])]
    for i in range(2):
        o += [obs(f"telegram:t{i}", STRONG), obs(f"telegram:t{i}", "neutre", links=[cible])]
    o.append(obs("web:seul.test", STRONG + " " + STRONG))  # fort sémantiquement, jamais cité
    res = assign(o, cfg)
    export_csv(res, cfg, "R", tmp_path / "o")
    rows = {
        r["primary_ref"]: r
        for r in csv.DictReader(
            (tmp_path / "o" / "entites.csv").open(encoding="utf-8"), delimiter=";"
        )
    }
    assert rows["web:cible.test"]["validated_via_crossref"] == "oui"
    assert rows["web:cible.test"]["confidence_level"] in (
        "confirmé",
        "probable",
    )  # selon sa confiance
    seul = rows["web:seul.test"]
    assert float(seul["confidence_score"]) >= cfg.scoring.niveau_confirme
    assert seul["validated_via_crossref"] == "non" and seul["confidence_level"] == "probable"
