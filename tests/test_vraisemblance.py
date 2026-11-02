"""Indice de fiabilité par rapport de vraisemblance (D64, étape 3) : bornes, bandes, poids Admiralty, variantes."""

import pytest

from grappes import vraisemblance as vr
from grappes.variantes import regrouper


def test_poids_par_lettre_decroissant_et_a_vaut_un():
    p = [vr.poids_lettre(x) for x in "ABCDEF"]
    assert p[0] == 1.0 and p == sorted(p, reverse=True) and p[-1] > 0
    assert vr.poids_lettre("?") == vr.poids_lettre("F")


def test_total_borne_element_et_total():
    cfg = vr.charger()
    e = [vr.Element("x", "s", "A", 10.0), vr.Element("x", "s", "A", 10.0), vr.Element("x", "s", "A", 10.0)]
    assert vr.total(e, cfg) == cfg.borne_totale  # 3 + 3 + 3 -> borné à 6
    assert vr.total([vr.Element("x", "s", "A", -10.0)], cfg) == -cfg.borne_element
    assert vr.total([], cfg) == 0.0


def test_les_log10_s_additionnent():
    cfg = vr.charger()
    e = [vr.Element("a", "s", "A", 0.5), vr.Element("b", "s", "B", 1.0)]
    assert vr.total(e, cfg) == pytest.approx(1.5)


def test_bandes_verbales_ENFSI():
    cfg = vr.charger()
    assert vr.bande(0.0, cfg) == "aucun apport"
    assert vr.bande(0.5, cfg) == "soutien faible"
    assert vr.bande(1.5, cfg) == "soutien modéré"
    assert vr.bande(3.5, cfg) == "soutien fort"
    assert vr.bande(7.0, cfg) == "soutien extrêmement fort"
    assert vr.bande(-2.0, cfg).startswith("soutien contre")


def test_surcharge_de_configuration():
    cfg = vr.charger().avec(nom_dans_texte=2.0)
    assert cfg.v("nom_dans_texte") == 2.0 and vr.charger().v("nom_dans_texte") != 2.0


def test_variantes_nomenklatura():
    noms = ["Fenix Toulouse Handball", "Fenix Toulouse", "Metz Handball", "Boulangerie Dupont"]
    groupes = [[noms[i] for i in g] for g in regrouper(noms)]
    assert ["Fenix Toulouse Handball", "Fenix Toulouse"] in groupes
    assert ["Boulangerie Dupont"] in groupes and ["Metz Handball"] in groupes


def test_rprec_ex_aequo_espere():
    from grappes.autocal.evaluation import rprec_ex_aequo

    # R = 2 ; a > (b = c = d) ; b et d pertinents : une place reste pour 3 ex æquo dont 2 pertinents
    run = {"a": 5.0, "b": 3.0, "c": 3.0, "d": 3.0}
    assert rprec_ex_aequo(run, {"a", "b", "d"}, 2) == pytest.approx((1 + 1 * 2 / 3) / 2)
    assert rprec_ex_aequo({}, {"x"}, 3) == 0.0
    # sans ex æquo à la frontière : valeur ordinaire
    assert rprec_ex_aequo({"a": 3.0, "b": 2.0, "c": 1.0}, {"a", "c"}, 2) == pytest.approx(0.5)
