"""Code Admiralty (OTAN 6×6) : fiabilité des sources et crédibilité de l'information (DECISION_Q24.md § 1)."""

import csv
import dataclasses
import math
from pathlib import Path

import pytest

from grappes.clustering.assign import assign, credibility
from grappes.clustering.export import export_csv
from grappes.config import ConfigError, load_config
from grappes.fiabilite import (
    CREDIBILITY,
    LETTERS,
    FiabiliteError,
    Reliability,
    explain,
    letter_factors,
    load_fiabilite,
    roc_weights,
)
from grappes.pipeline import run
from grappes.storage.db import Database
from test_phase4 import STRONG, STRONG_B, make_cfg, obs

ROOT = Path(__file__).resolve().parents[1]
TABLE = load_fiabilite(ROOT / "config" / "fiabilite_sources.yaml")


def cfg_admiralty(tmp_path, **scoring):
    tmp_path.mkdir(exist_ok=True)
    cfg = make_cfg(tmp_path, seuil_min=40, **scoring)
    return dataclasses.replace(cfg, fiabilite=TABLE)


# --- poids dérivés d'une méthode publiée, jamais saisis ---------------------------------------------------


def test_rang_centroide_formule_publiee():
    """Barron & Barrett (1996) : w_i = (1/n)·Σ_{k=i..n} 1/k ; pour n = 3 : 11/18, 5/18, 2/18."""
    assert roc_weights(3) == pytest.approx([11 / 18, 5 / 18, 2 / 18])
    w = roc_weights(6)
    assert sum(w) == pytest.approx(1.0) and w == sorted(w, reverse=True)
    assert w[0] == pytest.approx((1 + 1 / 2 + 1 / 3 + 1 / 4 + 1 / 5 + 1 / 6) / 6)


def test_facteurs_relatifs_au_web_generique():
    f = letter_factors("D")
    assert f["D"] == 1.0  # le comportement historique (poids 1) est celui d'une source D
    assert [f[x] for x in LETTERS] == sorted(
        (f[x] for x in LETTERS), reverse=True
    )  # A > B > ... > F, strictement
    assert len(set(f.values())) == 6
    w = roc_weights(6)
    assert f["A"] == pytest.approx(w[0] / w[3]) and f["F"] == pytest.approx(w[5] / w[3])
    assert TABLE.factors == pytest.approx(
        f
    )  # le fichier ne contient AUCUN poids : tout vient de la méthode


def test_le_fichier_de_fiabilite_ne_contient_aucun_poids_numerique():
    import yaml

    d = yaml.safe_load((ROOT / "config" / "fiabilite_sources.yaml").read_text("utf-8"))
    assert set(d["poids"]) == {"methode", "reference"}
    assert all(
        isinstance(v, str) for v in d["ecosystemes"].values()
    )  # des lettres, pas des nombres


def test_echelle_de_la_norme_en_francais():
    assert set(CREDIBILITY) == {1, 2, 3, 4, 5, 6} and CREDIBILITY[1].startswith("Confirmée")


# --- lettres par type de fournisseur --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "lettre"),
    [
        ("wikipedia:fr/udf", "C"), ("web:blog-inconnu.test", "D"), ("rss:flux-inconnu.test", "D"),
        ("rss:letudiant.fr", "B"), ("web:www.lemonde.fr", "B"), ("web:actu.liberation.fr", "B"),
        ("x:compte", "E"), ("telegram:canal", "E"), ("instagram:compte", "E"), ("nouveau:truc", "F"),
    ],
)  # fmt: skip
def test_lettre_par_type_de_fournisseur(ref, lettre):
    assert Reliability(TABLE).letter(ref) == lettre


def test_registre_officiel_place_en_a():
    r = Reliability(TABLE, registre=frozenset({"web:association.test"}))
    assert r.letter("web:association.test") == "A" and r.letter("web:autre.test") == "D"
    assert r.factor("web:association.test") == pytest.approx(TABLE.factors["A"])
    assert explain(r, "web:association.test")["raison"] == "registre officiel"


def test_table_absente_ou_invalide(tmp_path):
    neutre = load_fiabilite(tmp_path / "absent.yaml")
    assert (
        not neutre.enabled
        and Reliability(neutre).factor("x:compte") == 1.0
        and Reliability(neutre).letter("x:a") == "D"
    )
    for contenu in (
        "poids: {reference: Z}\n",
        "ecosystemes: {web: Q}\n",
        "medias_etablis: oups\n",
        "defaut: 9\n",
        "poids: {methode: inventee}\n",
    ):
        (tmp_path / "f.yaml").write_text(contenu, "utf-8")
        with pytest.raises(FiabiliteError):
            load_fiabilite(tmp_path / "f.yaml")
    (tmp_path / "grappes").mkdir()
    (tmp_path / "grappes" / "g.yaml").write_text("id: G\nlexicon: {terms: {a: 1}}\n")
    (tmp_path / "fiabilite_sources.yaml").write_text("defaut: 9\n", "utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_la_table_entre_dans_config_hash(tmp_path):
    (tmp_path / "grappes").mkdir()
    (tmp_path / "grappes" / "g.yaml").write_text("id: G\nlexicon: {terms: {a: 1}}\n")
    h0 = load_config(tmp_path).config_hash
    (tmp_path / "fiabilite_sources.yaml").write_text(
        (ROOT / "config" / "fiabilite_sources.yaml").read_text("utf-8"), "utf-8"
    )
    assert load_config(tmp_path).config_hash != h0  # les poids changent les scores : le run_id suit


# --- recoupement : le poids accélère, ne contourne jamais la condition des 2 écosystèmes -------------------


def scene(tmp_path, citants, registre=()):
    """`citants` : liste de clés de sources membres (texte fort) qui citent toutes la cible `web:cible.test`."""
    cfg = cfg_admiralty(tmp_path)
    o = [obs("web:cible.test", "rien de précis")]
    for ref in citants:
        o += [obs(ref, STRONG), obs(ref, "neutre", links=["https://cible.test/p"])]
    rel = Reliability(TABLE, registre=frozenset(registre))
    return assign(o, cfg, reliability=rel)


def rec(res):
    return next(r for r in res.validations if r.cited == "web:cible.test" and r.grappe_id == "G-A")


def test_une_source_a_et_une_source_d_independante_confirment_avec_deux_citants(tmp_path):
    res = scene(
        tmp_path / "ad", ["web:registre.test", "rss:inconnu.test"], registre=["web:registre.test"]
    )
    r = rec(res)
    assert r.validated and len(r.citers) == 2 and set(r.ecosystems) == {"web", "rss"}
    assert r.weight_sum == pytest.approx(TABLE.factors["A"] + TABLE.factors["D"])


def test_deux_sources_d_ne_suffisent_pas_il_en_faut_trois(tmp_path):
    deux = rec(scene(tmp_path / "dd", ["web:d1.test", "rss:d2.test"]))
    assert not deux.validated and "citants membres 2 < 3" in " ".join(
        deux.reasons
    )  # comme avant l'Admiralty
    trois = rec(scene(tmp_path / "ddd", ["web:d1.test", "rss:d2.test", "web:d3.test"]))
    assert (
        trois.validated and len(trois.citers) == 3
    )  # une source A converge plus vite : 2 citants au lieu de 3


def test_un_media_etabli_et_une_source_d_confirment_aussi(tmp_path):
    r = rec(scene(tmp_path / "bd", ["web:lemonde.fr", "rss:inconnu.test"]))
    assert r.validated and r.weight_sum == pytest.approx(TABLE.factors["B"] + 1.0)  # 2,35 + 1 >= 3


def test_wikipedia_c_et_source_d_ne_suffisent_pas(tmp_path):
    r = rec(scene(tmp_path / "cd", ["wikipedia:fr/article", "web:d1.test"]))
    assert not r.validated and "poids" in " ".join(r.reasons)  # 1,54 + 1 < 3


@pytest.mark.parametrize("citants", [
    ["web:a1.test"],  # une seule source A
    ["web:a1.test", "web:a2.test"],  # deux sources A, un seul écosystème
    ["web:a1.test", "web:a2.test", "web:a3.test", "web:a4.test"],  # beaucoup de sources A, un seul écosystème
])  # fmt: skip
def test_aucune_source_meme_a_ne_confirme_sans_deux_ecosystemes(tmp_path, citants):
    registre = [c for c in citants]
    r = rec(scene(tmp_path / f"a{len(citants)}", citants, registre=registre))
    assert not r.validated and any("écosystèmes 1 < 2" in x for x in r.reasons)
    assert r.weight_sum >= TABLE.factors["A"]  # le poids est là, et ne contourne rien


def test_poids_uniformes_comportement_historique_inchange(tmp_path):
    """Table absente : facteurs 1, effectif minimal = min_citers : exactement les règles d'avant."""
    tmp_path.mkdir(exist_ok=True)
    cfg = make_cfg(tmp_path, seuil_min=40)
    o = [obs("web:cible.test", "rien")]
    for ref in ("web:d1.test", "rss:d2.test"):
        o += [obs(ref, STRONG), obs(ref, "neutre", links=["https://cible.test/p"])]
    r = rec(assign(o, cfg))
    assert not r.validated and "citants membres 2 < 3" in " ".join(r.reasons)


# --- crédibilité de l'information (1 à 6) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statut", "valide", "citants", "oppose", "conf", "attendu"),
    [
        ("classé", True, 3, False, 90, 1), ("classé", False, 1, False, 60, 2), ("classé", False, 0, False, 60, 3),
        ("ambigu", False, 0, False, 60, 4), ("ambigu", False, 0, True, 60, 5), ("non_classé", False, 0, False, 20, 4),
        ("secondaire", False, 0, False, 20, 4), ("non_classé", False, 0, False, 0, 6),
    ],
)  # fmt: skip
def test_credibilite_deduite_des_signaux(statut, valide, citants, oppose, conf, attendu):
    assert credibility(statut, valide, citants, oppose, conf) == attendu


def test_credibilites_sur_un_cas_reel_de_recoupement(tmp_path):
    cfg = cfg_admiralty(tmp_path / "c")
    o = [obs("web:cible.test", "soupe")]
    for ref in ("web:lemonde.fr", "rss:d2.test"):
        o += [obs(ref, STRONG), obs(ref, "neutre", links=["https://cible.test/p"])]
    o += [obs("web:seul.test", STRONG)]  # rangée d'après son seul texte
    o += [
        obs("web:cite-un-membre.test", STRONG),
        obs("web:cite-un-membre.test", "neutre", links=["https://lonely.test/"]),
    ]
    o += [obs("web:lonely.test", STRONG)]  # citée par un membre, sans recoupement complet
    o += [obs("web:mixte.test", STRONG + " " + STRONG_B)]  # grappes opposées A et B à égalité
    res = assign(o, cfg)
    assert res.admiralty["web:cible.test"] == "D1"  # recoupée sur 2 écosystèmes
    assert res.admiralty["web:seul.test"] == "D3"
    assert res.admiralty["web:lonely.test"] == "D2"
    assert res.admiralty["web:mixte.test"] == "D5"  # contredite : ambiguïté avec une grappe opposée
    assert res.admiralty["web:lemonde.fr"].startswith("B")
    cred = {c.cited: c.credibility for c in res.citations}
    assert cred["web:cible.test"] == 1
    assert {c.citer: c.reliability for c in res.citations}["web:lemonde.fr"] == "B"


def test_aucune_information_credibilite_6(tmp_path):
    cfg = cfg_admiralty(tmp_path)
    res = assign([obs("web:muet.test", "bonjour demain")], cfg)
    assert res.admiralty["web:muet.test"] == "D6"


# --- traçabilité : observations, citations, appartenances, entites.csv -------------------------------------------


def test_champs_admiralty_en_base_et_dans_entites_csv(tmp_path):
    cfg = cfg_admiralty(tmp_path / "c")
    db = Database(tmp_path / "a.db")
    db.add_observations(
        [
            obs("web:lemonde.fr", STRONG),
            obs("web:blog.test", STRONG, links=["https://lemonde.fr/"]),
        ],
        {},
        letter=Reliability(TABLE).letter,
    )
    _, res = run(db, cfg)
    assert {r["source_key"]: r["fiabilite_admiralty"] for r in db.conn.execute("SELECT source_key, fiabilite_admiralty FROM observations")} == {
        "web:lemonde.fr": "B", "web:blog.test": "D",
    }  # fmt: skip
    m = db.conn.execute(
        "SELECT fiabilite_admiralty, credibilite_admiralty FROM memberships LIMIT 1"
    ).fetchone()
    assert m[0] in LETTERS and 1 <= m[1] <= 6
    c = db.conn.execute(
        "SELECT fiabilite_admiralty, credibilite_admiralty FROM citations WHERE citer='web:blog.test'"
    ).fetchone()
    assert c[0] == "D" and 1 <= c[1] <= 6
    export_csv(res, cfg, "R", tmp_path / "o")
    rows = {
        r["primary_ref"]: r
        for r in csv.DictReader(
            (tmp_path / "o" / "entites.csv").open(encoding="utf-8"), delimiter=";"
        )
    }
    assert (
        rows["web:lemonde.fr"]["admiralty"].startswith("B")
        and len(rows["web:lemonde.fr"]["admiralty"]) == 2
    )


def test_resultat_csv_inchange_une_colonne(tmp_path):
    from grappes.clustering.resultat import write_resultat

    cfg = cfg_admiralty(tmp_path / "c")
    res = assign([obs("web:s.test", STRONG)], cfg, names={"web:s.test": "Le Site"})
    p = write_resultat(res, cfg, tmp_path / "o")[0]
    assert (
        p.read_text("utf-8") == "NOM\nLe Site\n"
    )  # choix : le niveau Admiralty reste dans entites.csv (D50)


def test_run_id_depend_des_registres_et_du_classement(tmp_path):
    cfg = cfg_admiralty(tmp_path / "c")
    db = Database(tmp_path / "r.db")
    db.add_observations([obs("web:s.test", STRONG)], {})
    r1, _ = run(db, cfg)
    assert r1 == run(db, cfg)[0]
    sans = Reliability(TABLE)
    avec = Reliability(TABLE, registre=frozenset({"web:s.test"}))
    assert sans.fingerprint != avec.fingerprint and math.isfinite(avec.factor("web:s.test"))
