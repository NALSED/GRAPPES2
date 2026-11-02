"""Classement de production (D64) : calibreur, lignes de sortie, activation par la configuration. Aucun réseau."""

import math

from grappes import vraisemblance as vr
from grappes.classement import production as pr
from grappes.classement.candidats import Candidat


def test_calibreur_par_defaut_est_un_rapport_de_cotes_un_pour_un():
    c = pr.Calibreur()
    assert c.proba(0.0) == 0.5 and abs(c.proba(1.0) - 10 / 11) < 1e-9 and c.valide is False


def test_calibreur_isotone_interpole_et_borne():
    c = pr.Calibreur("isotone", x=[0.0, 1.0, 2.0], y=[0.1, 0.3, 0.9], valide=True)
    assert c.proba(-5) == 0.1 and c.proba(9) == 0.9 and math.isclose(c.proba(0.5), 0.2)


def test_config_absente_inactive(tmp_path):
    cc = pr.charger_config(tmp_path)
    assert cc.actif is False and cc.remplace_resultat is False and cc.variantes is False


def test_config_du_depot_inactive_par_defaut():
    cc = pr.charger_config("config")
    assert cc.actif is False and "wikidata" in cc.registres and cc.variantes is False


def test_lignes_disent_si_le_chiffre_est_une_mesure():
    lr = vr.charger()
    c = Candidat("reg:wikidata:Q1", "Fenix Toulouse Handball", "x", 0, "registre", 0, 0, 0, registres={"wikidata"}, ville="Toulouse")
    cls = [(c, 2.0, 0.999)]
    ls = pr.lignes(cls, pr.ClassementConfig(), lr, pr.Calibreur(), "FR")
    assert ls[0].calibre == "estimation étendue" and ls[0].score == 99.0 and ls[0].ville == "Toulouse"
    ls = pr.lignes(cls, pr.ClassementConfig(), lr, pr.Calibreur(valide=True), "FR")
    assert ls[0].calibre == "mesuré"
    assert pr.lignes(cls, pr.ClassementConfig(p_min=0.9999), lr, pr.Calibreur(), "FR") == []
