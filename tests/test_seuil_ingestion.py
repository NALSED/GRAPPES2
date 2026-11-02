"""Seuil d'ingestion auto-ajustable, borné (DECISIONS_SEDNAL.md Q23)."""

import dataclasses

import pytest
import yaml

from grappes.autonomy import Autonomy, load_autonomy
from grappes.config import ConfigError, load_config
from grappes.cycle.learn import adjust_ingestion
from grappes.learned import (
    load_ingestion_threshold,
    load_learned_thresholds,
    save_ingestion_threshold,
    save_learned_thresholds,
)


def auto(**kw):
    base = {"seuil_ingestion": 20.0, "seuil_ingestion_min": 5.0, "seuil_ingestion_max": 40.0,
            "seuil_ingestion_auto": True, "variation_max": 5.0}  # fmt: skip
    return Autonomy(**{**base, **kw})


# --- configuration ------------------------------------------------------------------------------------


def test_forme_mapping_de_sednal(tmp_path):
    (tmp_path / "autonomie.yaml").write_text(
        "seuil_ingestion: {valeur: 20, min: 5, max: 40, auto_ajustable: true}\n", "utf-8"
    )
    a = load_autonomy(tmp_path)
    assert (
        a.seuil_ingestion,
        a.seuil_ingestion_min,
        a.seuil_ingestion_max,
        a.seuil_ingestion_auto,
    ) == (20, 5, 40, True)


def test_forme_scalaire_non_ajustable(tmp_path):
    (tmp_path / "autonomie.yaml").write_text("seuil_ingestion: 30\n", "utf-8")
    a = load_autonomy(tmp_path)
    assert a.seuil_ingestion == 30 and not a.seuil_ingestion_auto
    save_ingestion_threshold(tmp_path, 12, 1)
    assert (
        load_autonomy(tmp_path).seuil_ingestion == 30
    )  # valeur apprise ignorée tant que non autorisé


def test_valeur_apprise_reprise_et_ramenee_dans_les_bornes(tmp_path):
    (tmp_path / "autonomie.yaml").write_text(
        "seuil_ingestion: {valeur: 20, min: 5, max: 40, auto_ajustable: true}\n", "utf-8"
    )
    assert load_autonomy(tmp_path).seuil_ingestion == 20  # rien d'appris : valeur de départ
    save_ingestion_threshold(tmp_path, 25, 1)
    assert load_autonomy(tmp_path).seuil_ingestion == 25
    save_ingestion_threshold(tmp_path, 99, 2)
    assert load_autonomy(tmp_path).seuil_ingestion == 40  # jamais au-delà de la borne
    save_ingestion_threshold(tmp_path, 1, 3)
    assert load_autonomy(tmp_path).seuil_ingestion == 5


@pytest.mark.parametrize(
    "bloc",
    [
        "{valeur: 20, min: 50, max: 40}",
        "{valeur: 20, min: -1, max: 40}",
        "{valeur: 20, min: 5, max: 400}",
        "{valeur: abc}",
    ],
)
def test_bornes_invalides(tmp_path, bloc):
    (tmp_path / "autonomie.yaml").write_text(f"seuil_ingestion: {bloc}\n", "utf-8")
    with pytest.raises(ConfigError):
        load_autonomy(tmp_path)


def test_le_fichier_des_seuils_garde_les_deux_familles(tmp_path):
    save_learned_thresholds(tmp_path, {"seuil_min": 35.0, "marge_ambiguite": 8.0}, 1)
    save_ingestion_threshold(tmp_path, 25, 2)
    assert load_ingestion_threshold(tmp_path) == 25 and load_learned_thresholds(tmp_path) == {
        "seuil_min": 35.0,
        "marge_ambiguite": 8.0,
    }
    save_learned_thresholds(
        tmp_path, {"seuil_min": 30.0}, 3
    )  # un recalibrage ne perd pas le seuil d'ingestion
    assert load_ingestion_threshold(tmp_path) == 25


def test_le_seuil_d_ingestion_n_entre_pas_dans_config_hash(tmp_path):
    (tmp_path / "grappes").mkdir()
    (tmp_path / "grappes" / "g.yaml").write_text("id: G\nlexicon: {terms: {a: 1}}\n")
    h0 = load_config(tmp_path).config_hash
    save_ingestion_threshold(tmp_path, 30, 1)  # il ne change aucun score : le run_id reste stable
    assert load_config(tmp_path).config_hash == h0


# --- décision d'ajustement ----------------------------------------------------------------------------


def refs(n):
    return [f"web:s{i}.test" for i in range(n)]


def test_trop_peu_de_sources_pour_juger():
    r = adjust_ingestion(auto(), refs(2), set(), 20, 10)
    assert not r.changed and r.after == 20 and "trop peu" in r.reason


def test_bruit_le_seuil_monte_de_variation_max():
    r = adjust_ingestion(auto(), refs(6), {"web:s0.test"}, 30, 5)  # 1 source rangée sur 6
    assert (
        r.changed
        and (r.before, r.after) == (20, 25)
        and "trop de bruit" in r.reason
        and "1 source(s) sur 6" in r.reason
    )


def test_seuil_assoupli_si_tout_est_pertinent_et_beaucoup_refuse():
    r = adjust_ingestion(auto(), refs(5), set(refs(5)), 20, 8)  # 100 % rangées, 40 % refusées
    assert (r.before, r.after) == (20, 15) and "probablement écartées" in r.reason


def test_pertinence_satisfaisante_ou_peu_de_refus_inchange():
    assert not adjust_ingestion(auto(), refs(6), set(refs(4)), 20, 2).changed  # 67 %
    r = adjust_ingestion(auto(), refs(5), set(refs(5)), 100, 5)  # 100 % mais seulement 5 % refusés
    assert not r.changed and "satisfaisante" in r.reason


@pytest.mark.parametrize(("depart", "attendu"), [(38.0, 40.0), (40.0, 40.0)])
def test_jamais_au_dela_de_la_borne_haute(depart, attendu):
    r = adjust_ingestion(auto(seuil_ingestion=depart), refs(6), set(), 30, 5)
    assert r.after == attendu
    if depart == 40.0:
        assert not r.changed and "borne" in r.reason


@pytest.mark.parametrize(("depart", "attendu"), [(7.0, 5.0), (5.0, 5.0)])
def test_jamais_sous_la_borne_basse(depart, attendu):
    r = adjust_ingestion(auto(seuil_ingestion=depart), refs(5), set(refs(5)), 10, 6)
    assert r.after == attendu


def test_non_autorise_jamais_modifie():
    r = adjust_ingestion(auto(seuil_ingestion_auto=False), refs(6), set(), 30, 5)
    assert not r.changed and "non autorisé" in r.reason


def test_doublons_de_sources_comptes_une_fois():
    r = adjust_ingestion(auto(), ["web:a.test"] * 10, set(), 30, 5)  # une seule source distincte
    assert not r.changed and "trop peu" in r.reason


def test_dataclass_autonomy_accepte_les_champs():
    assert dataclasses.replace(auto(), seuil_ingestion=33.0).seuil_ingestion == 33.0
    assert yaml.safe_load("a: 1") == {"a": 1}
