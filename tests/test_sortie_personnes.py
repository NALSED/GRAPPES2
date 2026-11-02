"""Profils de sortie (D64) et périmètre des personnes préparé mais inactif."""

import pytest

from grappes.config import ConfigError
from grappes.personnes import PerimetrePersonnes, load_perimetre
from grappes.sortie import LigneSortie, charger_profils, ecrire


def test_profils_par_defaut_sans_fichier(tmp_path):
    ids = [p.id for p in charger_profils(tmp_path)]
    assert ids == ["resultat", "tsites"]


def test_profils_du_depot_et_ecriture(tmp_path):
    profils = {p.id: p for p in charger_profils("config")}
    assert {"resultat", "tsites", "brut"} <= set(profils)
    lignes = [LigneSortie("Metz Handball", "Metz", "FR", 87.4, "soutien fort", "mesuré", ["web:x.fr"], ["wikidata"], "retenu")]
    chemins = ecrire(lignes, list(profils.values()), tmp_path, ignorer=frozenset({"resultat", "tsites"}))
    assert [p.name for p in chemins] == ["resultat_brut.csv"]
    txt = chemins[0].read_text(encoding="utf-8").splitlines()
    assert txt[0].startswith("nom;ville;pays;score") and "Metz Handball;Metz;FR;87;soutien fort;mesuré;wikidata;web:x.fr;retenu" in txt[1]


def test_colonne_inconnue_refusee(tmp_path):
    (tmp_path / "sortie.yaml").write_text("profils:\n  x: {fichier: x.csv, colonnes: [nom, secret]}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        charger_profils(tmp_path)


def test_profil_desactive_supprime_le_fichier(tmp_path):
    (tmp_path / "x.csv").write_text("vieux", encoding="utf-8")
    (tmp_path / "sortie.yaml").write_text("profils:\n  x: {actif: false, fichier: x.csv, colonnes: [nom]}\n", encoding="utf-8")
    assert ecrire([], charger_profils(tmp_path), tmp_path) == [] and not (tmp_path / "x.csv").exists()


def test_personnes_inactif_par_defaut_et_jamais_les_particuliers(tmp_path):
    assert load_perimetre(tmp_path / "absent.yaml") == PerimetrePersonnes()
    cfg = load_perimetre("config/personnes.yaml")
    assert cfg.collecte is False and cfg.perimetre == "roles_publics" and cfg.masquage is True
    (tmp_path / "p.yaml").write_text("perimetre: tous\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_perimetre(tmp_path / "p.yaml")
