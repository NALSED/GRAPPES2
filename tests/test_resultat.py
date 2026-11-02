"""`sorties/resultat.csv` : une seule colonne NOM (FINAL.md § 9)."""

import csv

import pytest

from grappes.clustering.assign import assign
from grappes.clustering.resultat import dedup_key, readable_name, write_resultat
from grappes.personnes import is_institution
from test_phase4 import STRONG, STRONG_B, make_cfg, obs


def lire(path):
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # UTF-8 sans BOM
    return list(csv.reader(raw.decode("utf-8").splitlines()))


def sortie(tmp_path, observations, names, **scoring):
    cfg = make_cfg(tmp_path / "cfg", **{"seuil_min": 30, **scoring})
    res = assign(observations, cfg, names=names)
    return write_resultat(res, cfg, tmp_path / "out"), res


@pytest.fixture
def cfgdir(tmp_path):
    (tmp_path / "cfg").mkdir()
    return tmp_path


def test_une_colonne_nom_tri_alphabetique_sans_accents(cfgdir):
    o = [obs(f"web:{d}", STRONG) for d in ("z.test", "e.test", "a.test")]
    names = {"web:z.test": "Zèbre Info", "web:e.test": "Éclair du soir", "web:a.test": "Albatros"}
    paths, _ = sortie(cfgdir, o, names)
    rows = lire(paths[0])
    assert paths[0].name == "resultat.csv" and rows[0] == ["NOM"]
    assert [r[0] for r in rows[1:]] == [
        "Albatros",
        "Éclair du soir",
        "Zèbre Info",
    ]  # É trié comme E
    assert all(len(r) == 1 for r in rows)  # une seule colonne, rien d'autre


def test_variantes_dedoublonnees_casse_accents_espaces_suffixes(cfgdir):
    o = [obs(f"web:{d}", STRONG) for d in ("a.test", "b.test", "c.test", "d.test")]
    names = {
        "web:a.test": "Café  Crème",
        "web:b.test": "CAFE CREME Officiel",
        "web:c.test": "café-crème",
        "web:d.test": "Autre Média",
    }
    rows = lire(sortie(cfgdir, o, names)[0][0])
    noms = [r[0] for r in rows[1:]]
    assert len(noms) == 2 and "Autre Média" in noms
    assert sum(1 for n in noms if dedup_key(n, ("officiel",)) == "cafecreme") == 1


def test_entite_sans_nom_lisible_exclue(cfgdir):
    o = [obs(f"web:{d}", STRONG) for d in ("a.test", "b.test", "c.test", "d.test", "e.test")]
    names = {
        "web:a.test": "https://a.test/page",  # URL brute
        "web:b.test": "b.test",  # domaine seul
        "web:c.test": "@compte",  # identifiant technique
        "web:d.test": "   ",  # vide
        # web:e.test : aucun nom connu
    }
    paths, res = sortie(cfgdir, o, names)
    assert lire(paths[0]) == [["NOM"]]  # tous exclus : en-tête seul
    assert len(res.entities) == 5


def test_fichier_vide_en_tete_seul(cfgdir):
    paths, _ = sortie(cfgdir, [], {})
    assert paths[0].read_bytes() == b"NOM\n"


def test_seuil_ambigus_et_non_classes(cfgdir):
    o = [
        obs("web:fort.test", STRONG),
        obs("web:faible.test", "soupe"),
        obs("web:rien.test", "bonjour"),
    ]
    names = {"web:fort.test": "Fort", "web:faible.test": "Faible", "web:rien.test": "Rien"}
    assert [r[0] for r in lire(sortie(cfgdir, o, names)[0][0])[1:]] == ["Fort"]
    sub = cfgdir / "b"
    (sub / "cfg").mkdir(parents=True)
    rows = lire(sortie(sub, o, names, resultat="{confiance_min: 90}")[0][0])
    assert rows == [["NOM"]]  # seuil configurable


def test_personnes_exclues_par_defaut_sauf_indice_institutionnel(cfgdir):
    o = [obs("x:jean_d", STRONG), obs("x:le_quotidien", STRONG), obs("telegram:canal_c", STRONG)]
    names = {
        "x:jean_d": "Jean Dupont",
        "x:le_quotidien": "Le Quotidien du Soir",
        "telegram:canal_c": "Canal C",
    }
    rows = lire(sortie(cfgdir, o, names)[0][0])
    assert [r[0] for r in rows[1:]] == ["Canal C", "Le Quotidien du Soir"]  # Jean Dupont absent
    assert "Jean Dupont" not in "".join(map(str, rows))


def test_en_cas_de_doute_c_est_une_personne(cfgdir):
    """Sans indice explicite, ou sans nom : traité comme une personne, donc exclu."""
    o = [obs("x:compte_sans_nom", STRONG), obs("x:marie_m", STRONG), obs("web:site.test", STRONG)]
    names = {"x:marie_m": "Marie Martin", "web:site.test": "Le Site"}
    rows = lire(sortie(cfgdir, o, names)[0][0])
    assert rows == [["NOM"], ["Le Site"]]


def test_inclure_personnes_les_met_en_clair(tmp_path):
    (tmp_path / "cfg").mkdir()
    o = [obs("x:jean_d", STRONG)]
    paths, _ = sortie(
        tmp_path, o, {"x:jean_d": "Jean Dupont"}, resultat="{inclure_personnes: true}"
    )
    assert lire(paths[0]) == [["NOM"], ["Jean Dupont"]]


def test_un_fichier_par_grappe(cfgdir):
    o = [obs("web:a.test", STRONG), obs("web:b.test", STRONG_B)]
    names = {"web:a.test": "Cuisine Info", "web:b.test": "Jardin Info"}
    paths, _ = sortie(cfgdir, o, names)
    assert sorted(p.name for p in paths) == [
        "resultat.csv",
        "resultat_G-A.csv",
        "resultat_G-B.csv",
        "resultat_tsites.csv",
    ]
    by = {p.name: [r[0] for r in lire(p)[1:]] for p in paths if p.name != "resultat_tsites.csv"}
    assert by["resultat_G-A.csv"] == ["Cuisine Info"] and by["resultat_G-B.csv"] == ["Jardin Info"]
    assert by["resultat.csv"] == ["Cuisine Info", "Jardin Info"]


def test_formule_tableur_neutralisee(cfgdir):
    paths, _ = sortie(cfgdir, [obs("web:a.test", STRONG)], {"web:a.test": "=SOMME(A1)"})
    assert lire(paths[0])[1][0][:1] == "'"


def test_helpers():
    assert readable_name("  Le   Journal\n") == "Le Journal"
    assert readable_name("exemple.org") is None and readable_name("12345") is None
    assert readable_name("t.me/canal") is None and readable_name(None) is None
    assert is_institution("Mairie de Quelquepart") and not is_institution("Jean Dupont")
    assert is_institution("Ville de Lyon") and not is_institution("Villette")
