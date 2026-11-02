import pytest

from grappes.semantic.scoring import Scorer, normalize, score_detail, score_proximity

KW = {"soupe": 1.0, "potager": 1.0, "recette": 1.0}


def test_texte_vide_et_aucun_terme():
    assert score_proximity("", KW) == 0
    assert score_proximity("rien à voir ici", KW) == 0
    assert score_proximity("soupe", {}) == 0


def test_monotonie_termes_distincts():
    s1 = score_proximity("soupe", KW)
    s2 = score_proximity("soupe potager", KW)
    s3 = score_proximity("soupe potager recette", KW)
    assert 0 < s1 < s2 < s3


def test_trois_termes_poids_1_formule():
    # 100 × (1 − e^(−3/3)) = 63.21 (la MISSION annonçait ≈ 86 : incohérent avec la formule, voir D7)
    assert score_proximity("soupe potager recette", KW) == pytest.approx(63.21, abs=0.01)
    assert score_proximity("soupe potager recette", KW, scale=1.5) == pytest.approx(86.47, abs=0.01)


def test_saturation():
    text = " ".join(KW) * 500
    assert score_proximity(text, {k: 50.0 for k in KW}) <= 100
    assert score_proximity(text, {k: 50.0 for k in KW}) > 99


def test_casse_et_accents_latins():
    assert score_proximity("SOUPE", {"soupé": 1}) == score_proximity("soupe", {"soupe": 1})
    assert score_proximity("Crème Brûlée", ["creme brulee"]) > 0


def test_cyrillique_et_armenien_non_alteres():
    assert score_proximity("Привет мир", ["привет"]) > 0
    assert normalize("й") == "й"  # le « й » cyrillique ne perd pas sa brève
    assert score_proximity("Բարեւ աշխարհ", ["բարեւ"]) > 0


def test_limites_de_mots():
    assert score_proximity("party", ["art"]) == 0
    assert score_proximity("un art ancien", ["art"]) > 0
    assert score_proximity("l'art", ["art"]) > 0


def test_repetition_sous_lineaire():
    one = score_detail("soupe", ["soupe"])
    ten = score_detail("soupe " * 10, ["soupe"])
    assert one.score < ten.score < 10 * one.score
    assert ten.matched == {"soupe": 10}


def test_penalite():
    base = score_proximity("soupe potager", KW)
    pen = score_proximity("soupe potager parasite", {**KW, "parasite": -1.0})
    assert pen < base
    assert score_proximity("parasite", {"parasite": -5.0}) == 0  # plancher à 0


def test_multi_mots_hashtag_handle():
    assert score_proximity("ma grande soupe", ["grande soupe"]) > 0
    assert score_proximity("voir #cuisine", ["#cuisine"]) > 0
    assert score_proximity("merci @chef_demo", ["@chef_demo"]) > 0
    assert (
        score_proximity("grande  soupe", ["grande soupe"]) == 0
    )  # espaces multiples : pas de magie


def test_scale():
    assert score_proximity("soupe", ["soupe"], scale=1.0) > score_proximity(
        "soupe", ["soupe"], scale=10.0
    )


def test_determinisme():
    a = score_detail("soupe potager soupe", KW)
    assert a == score_detail("soupe potager soupe", KW)


def test_protocole_scorer():
    s: Scorer = score_proximity
    assert s("soupe", ["soupe"]) > 0
