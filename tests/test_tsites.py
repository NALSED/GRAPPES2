"""Sortie de compatibilité `sorties/resultat_tsites.csv` : nom;ville;pays (DECISION_TSITES.md, D59)."""

import dataclasses

import pytest

from grappes.autonomy import Autonomy, load_autonomy
from grappes.clustering.assign import assign
from grappes.clustering.resultat import write_resultat
from grappes.config import ConfigError
from test_phase4 import STRONG, make_cfg, obs
from test_resultat import lire

NAMES = {
    "web:asso.test": "Association Jardins",
    "web:blog.test": "Blog Potager",
    "web:b.test": "Zèbre Info",
}
PLACES = {"web:asso.test": ("MONTLUCON", "FR"), "web:blog.test": ("", "")}


def sortie(tmp_path, auto=None, places=PLACES):
    (tmp_path / "cfg").mkdir()
    cfg = make_cfg(tmp_path / "cfg", seuil_min=30)
    o = [obs(r, STRONG) for r in NAMES]
    res = assign(o, cfg, names=NAMES)
    return write_resultat(
        res, cfg, tmp_path / "out", auto=auto or Autonomy(), places=places
    ), tmp_path / "out"


def test_format_exact(tmp_path):
    paths, out = sortie(tmp_path)
    f = out / "resultat_tsites.csv"
    assert f in paths
    raw = f.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # UTF-8 sans BOM
    assert (
        raw.decode("utf-8").splitlines()[0] == "nom;ville;pays"
    )  # en-tête exact, minuscules, point-virgule
    assert all(line.count(";") == 2 for line in raw.decode().splitlines())


def test_ville_renseignee_ou_vide_jamais_inventee(tmp_path):
    _, out = sortie(tmp_path)
    rows = {
        r[0]: r
        for r in (
            l.split(";") for l in (out / "resultat_tsites.csv").read_text("utf-8").splitlines()[1:]
        )
    }
    assert rows["Association Jardins"] == [
        "Association Jardins",
        "Montlucon",
        "FR",
    ]  # ville du registre
    assert rows["Blog Potager"][1] == "" and rows["Zèbre Info"][1] == ""  # inconnue : vide
    assert all(r[2] == "FR" for r in rows.values())  # pays par défaut


def test_memes_noms_meme_ordre_que_resultat(tmp_path):
    _, out = sortie(tmp_path)
    noms = [r[0] for r in lire(out / "resultat.csv")[1:]]
    tsites = [
        l.split(";")[0] for l in (out / "resultat_tsites.csv").read_text("utf-8").splitlines()[1:]
    ]
    assert noms == tsites and len(noms) == 3
    assert lire(out / "resultat.csv")[0] == ["NOM"]  # la sortie de référence ne change pas


def test_pays_defaut_configurable_et_pays_du_registre(tmp_path):
    auto = dataclasses.replace(Autonomy(), export_tsites_pays_defaut="BE")
    _, out = sortie(tmp_path, auto, {"web:asso.test": ("Liège", "FR")})
    rows = {
        l.split(";")[0]: l.split(";")
        for l in (out / "resultat_tsites.csv").read_text("utf-8").splitlines()[1:]
    }
    assert rows["Association Jardins"][2] == "FR" and rows["Blog Potager"][2] == "BE"


def test_actif_false_desactive_proprement(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "resultat_tsites.csv").write_text("ancien\n")  # fichier périmé
    auto = dataclasses.replace(Autonomy(), export_tsites_actif=False)
    paths, out = sortie(tmp_path, auto)
    assert not (out / "resultat_tsites.csv").exists() and all(
        p.name != "resultat_tsites.csv" for p in paths
    )
    assert (out / "resultat.csv").exists()


def test_configuration_lue_et_validee(tmp_path):
    (tmp_path / "autonomie.yaml").write_text("autonome: false\n")
    a = load_autonomy(tmp_path)
    assert a.export_tsites_actif is True and a.export_tsites_pays_defaut == "FR"  # défauts
    (tmp_path / "autonomie.yaml").write_text("export_tsites: {actif: false, pays_defaut: be}\n")
    a = load_autonomy(tmp_path)
    assert a.export_tsites_actif is False and a.export_tsites_pays_defaut == "BE"
    for bad in (
        "export_tsites: {pays_defaut: FRA}",
        "export_tsites: {actif: oui}",
        "export_tsites: 3",
    ):
        (tmp_path / "autonomie.yaml").write_text(bad + "\n")
        with pytest.raises(ConfigError):
            load_autonomy(tmp_path)
