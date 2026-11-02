"""Variantes : le lien « complet » ne chaîne pas (défaut « simple » inchangé). Noms inventés."""

import pytest

from grappes.variantes import regrouper

CHAINE = ["Zorbel Rivage", "Zorbel Rivage Club Nautique Sud", "Club Nautique Sud"]


def noms_des_groupes(groupes):
    return sorted(sorted(CHAINE[i] for i in g) for g in groupes)


def test_le_lien_simple_enchaine_des_noms_sans_rapport():
    # c'est le défaut documenté de l'historique : A≈B et B≈C suffisent, alors que A et C ont un score de 0
    assert len(regrouper(CHAINE)) == 1


def test_le_lien_complet_n_enchaine_pas():
    g = noms_des_groupes(regrouper(CHAINE, lien="complet"))
    assert len(g) == 2  # le milieu s'attache à UN côté (paire la plus sûre d'abord), l'autre reste seul
    assert not any("Zorbel Rivage" in x and "Club Nautique Sud" in x for x in g)  # les deux bouts ne sont jamais ensemble


def test_le_lien_complet_garde_les_vraies_variantes_ensemble():
    noms = ["Zorbel Rivage", "Zorbel Rivage Club", "Zorbel Rivage Club Nautique Sud"]
    assert len(regrouper(noms, lien="complet")) == 1


def test_lien_inconnu_refuse():
    with pytest.raises(ValueError, match="lien"):
        regrouper(CHAINE, lien="moyen")
