from datetime import UTC, datetime
from pathlib import Path

import pytest

from grappes.clustering.assign import assign
from grappes.config import load_config
from grappes.ecosystem.crossref import Citation, CrossRefRule, filter_window, validate_crossref
from grappes.models import Grappe, Lexicon, Observation, SourceRef
from grappes.semantic.relations import anchor_scores, are_opposed

TS = datetime(2026, 3, 1, tzinfo=UTC)
STRONG = "soupe soupe recette cuisine"  # lexique de G-A
STRONG_B = "potager potager semis jardinage"  # lexique de G-B
NEUTRAL = "rien de particulier"


def make_cfg(tmp_path: Path, **scoring):
    (tmp_path / "grappes").mkdir()
    base = {"top_n_observations": 1, "max_iter": 3, "seuil_min": 30, "redact_persons": "false"}
    base.update(scoring)
    lines = [f"{k}: {v}" for k, v in base.items()]
    (tmp_path / "scoring.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "grappes" / "a.yaml").write_text(
        "id: G-A\nlexicon: {terms: {soupe: 1, recette: 1, cuisine: 1}}\n"
        "anchors: ['x:anchor1', 'x:anchor2']\noppositions: [G-B]\n",
        encoding="utf-8",
    )
    (tmp_path / "grappes" / "b.yaml").write_text(
        "id: G-B\nlexicon: {terms: {potager: 1, semis: 1, jardinage: 1}}\n", encoding="utf-8"
    )
    return load_config(tmp_path)


_n = 0


def obs(src: str, text: str, mentions=(), links=()):
    global _n
    _n += 1
    eco, ident = src.split(":", 1)
    return Observation(
        obs_id=f"o{_n}",
        source=SourceRef(eco, ident),
        ts=TS,
        text=text,
        mentions=tuple(SourceRef(*m.split(":", 1)) for m in mentions),
        links=tuple(links),
    )


def by_ref(res):
    ids = {e.primary_ref: e.entity_id for e in res.entities}
    main = {}
    for m in res.memberships:
        if m["status"] != "secondaire":
            main[m["entity_id"]] = m
    return lambda ref: main.get(ids[ref])


def members_citing(target: str, refs=("x:a", "x:b", "telegram:c")):
    out = [obs(r, STRONG) for r in refs]
    out += [obs(r, NEUTRAL, mentions=[target]) for r in refs]
    return out


def test_valide_par_recoupement(tmp_path):
    cfg = make_cfg(tmp_path)
    res = assign(members_citing("x:cible"), cfg)
    m = by_ref(res)("x:cible")
    assert m["grappe_id"] == "G-A" and m["via"] == "recoupement"
    assert m["confidence"] >= cfg.scoring.crossref_floor and m["status"] == "classé"
    rec = next(v for v in res.validations if v.cited == "x:cible" and v.grappe_id == "G-A")
    assert rec.validated and len(rec.citers) == 3 and set(rec.ecosystems) == {"x", "telegram"}
    assert rec.reasons[0].startswith("validé")


def test_refuse_un_seul_ecosysteme(tmp_path):
    cfg = make_cfg(tmp_path)
    res = assign(members_citing("x:cible", refs=("x:a", "x:b", "x:d")), cfg)
    rec = next(v for v in res.validations if v.cited == "x:cible" and v.grappe_id == "G-A")
    assert not rec.validated and any("écosystèmes" in r for r in rec.reasons)
    m = by_ref(res)("x:cible")
    assert m is None or m["via"] != "recoupement"


def test_refuse_pas_assez_de_citants_ni_de_poids(tmp_path):
    cfg = make_cfg(tmp_path)
    res = assign(members_citing("x:cible", refs=("x:a", "telegram:c")), cfg)
    rec = next(v for v in res.validations if v.cited == "x:cible" and v.grappe_id == "G-A")
    assert not rec.validated
    assert any("citants" in r for r in rec.reasons) and any("poids" in r for r in rec.reasons)


def test_citants_non_membres_ne_comptent_pas(tmp_path):
    cfg = make_cfg(tmp_path)
    o = [obs(r, NEUTRAL, mentions=["x:cible"]) for r in ("x:a", "x:b", "telegram:c")]
    res = assign(o, cfg)
    rec = next(v for v in res.validations if v.cited == "x:cible" and v.grappe_id == "G-A")
    assert not rec.validated and rec.citers == () and any("non membres" in r for r in rec.reasons)


def test_un_citant_une_voix_et_autocitation_ignoree(tmp_path):
    cfg = make_cfg(tmp_path)
    o = [obs("x:a", STRONG)] + [obs("x:a", NEUTRAL, mentions=["x:cible", "x:a"]) for _ in range(10)]
    res = assign(o, cfg)
    rec = next(v for v in res.validations if v.cited == "x:cible" and v.grappe_id == "G-A")
    assert rec.citers == ("x:a",) and not rec.validated
    assert not any(c.cited == "x:a" and c.citer == "x:a" for c in res.citations)


def test_poids_override_et_regle_min_citers(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    cfg = make_cfg(d)
    rule = CrossRefRule(min_citers=2, min_ecosystems=2, min_weight_sum=3.0)
    cits = [
        Citation("x:a", "x:t", "x", "o1", TS),
        Citation("telegram:c", "x:t", "telegram", "o2", TS),
    ]
    light = validate_crossref(cits, "G-A", lambda r: True, lambda r: 1.0, rule)["x:t"]
    heavy = validate_crossref(cits, "G-A", lambda r: True, lambda r: 2.0, rule)["x:t"]
    assert not light.validated and heavy.validated and heavy.weight_sum == 4.0
    assert cfg.scoring.crossref.min_citers == 3


def test_fenetre_temporelle():
    old = datetime(2025, 1, 1, tzinfo=UTC)
    cits = [
        Citation("x:a", "x:t", "x", "o1", old),
        Citation("x:b", "x:t", "x", "o2", TS),
        Citation("x:c", "x:t", "x", "o3", None),
    ]
    kept = filter_window(cits, 180)
    assert {c.obs_id for c in kept} == {"o2", "o3"}
    assert filter_window(cits, None) == cits


def test_ambigu_par_opposition(tmp_path):
    cfg = make_cfg(tmp_path)
    res = assign([obs("x:mixte", f"{STRONG} {STRONG_B}")], cfg)
    m = by_ref(res)("x:mixte")
    assert m["status"] == "ambigu"


def test_non_ambigu_si_ecart_net(tmp_path):
    cfg = make_cfg(tmp_path)
    res = assign([obs("x:net", f"{STRONG} {STRONG}"), obs("x:autre", STRONG_B)], cfg)
    assert by_ref(res)("x:net")["status"] == "classé"
    assert by_ref(res)("x:net")["grappe_id"] == "G-A"


def test_citer_un_opposant_ne_rapproche_pas(tmp_path):
    cfg = make_cfg(tmp_path)
    o = [obs(r, STRONG_B) for r in ("x:p", "x:q", "telegram:r")]
    o += [obs(r, NEUTRAL, mentions=["x:cible"]) for r in ("x:p", "x:q", "telegram:r")]
    res = assign(o, cfg)
    m = by_ref(res)("x:cible")
    # validé pour G-B (ses citants en sont membres), jamais pour G-A
    assert m["grappe_id"] == "G-B"
    assert not any(
        v.validated and v.grappe_id == "G-A" and v.cited == "x:cible" for v in res.validations
    )


def test_ancres(tmp_path):
    cfg = make_cfg(tmp_path)
    o = [
        obs("x:anchor1", NEUTRAL, mentions=["x:un", "x:deux"]),
        obs("x:anchor2", NEUTRAL, mentions=["x:deux"]),
    ]
    res = assign(o, cfg)
    ids = {e.primary_ref: e.entity_id for e in res.entities}
    anch = {(m["entity_id"], m["grappe_id"]): m["anchor"] for m in res.memberships}
    assert anch[ids["x:anchor1"], "G-A"] == 100.0
    assert anch[ids["x:un"], "G-A"] == 50.0
    assert anch[ids["x:deux"], "G-A"] == 100.0


def test_anchor_scores_unitaire():
    g = Grappe("G", "G", Lexicon({"a": 1.0}), anchors=("x:1", "x:2"))
    cits = [Citation("x:1", "x:t", "x", "o", None), Citation("x:t", "x:2", "x", "o", None)]
    s = anchor_scores(g, ["x:1", "x:t", "x:z"], cits)
    assert s == {"x:1": 100.0, "x:t": 100.0, "x:z": 0.0}


def test_opposition_symetrique():
    a = Grappe("A", "A", Lexicon({"a": 1.0}), oppositions=("B",))
    b = Grappe("B", "B", Lexicon({"b": 1.0}))
    assert are_opposed(a, b) and are_opposed(b, a)


def chaine():
    """a,b,c membres ; ils citent m (n'a que du texte neutre) ; puis a,b,m citent n."""
    o = [obs(r, STRONG) for r in ("x:a", "x:b", "telegram:c")]
    o += [obs(r, NEUTRAL, mentions=["telegram:m"]) for r in ("x:a", "x:b", "telegram:c")]
    o += [obs("telegram:m", NEUTRAL, mentions=["x:n"])]
    o += [obs(r, NEUTRAL, mentions=["x:n"]) for r in ("x:a", "x:b")]
    return o


def test_circularite_bornee_par_max_iter(tmp_path):
    # 1 itération : m validé (citants = seeds) mais n pas encore (m n'est pas encore membre)
    (tmp_path / "i1").mkdir()
    r1 = assign(chaine(), make_cfg(tmp_path / "i1", max_iter=1))
    v1 = {(v.cited, v.grappe_id): v.validated for v in r1.validations}
    assert v1[("telegram:m", "G-A")] and not v1[("x:n", "G-A")]
    assert r1.iterations == 1
    # 3 itérations : m devenu membre, il compte comme citant de n
    (tmp_path / "i3").mkdir()
    r3 = assign(chaine(), make_cfg(tmp_path / "i3", max_iter=3))
    v3 = {(v.cited, v.grappe_id): v.validated for v in r3.validations}
    assert v3[("x:n", "G-A")]
    assert r3.iterations <= 3


def test_anneau_de_non_membres_ne_s_auto_valide_pas(tmp_path):
    cfg = make_cfg(tmp_path)
    refs = ["x:a", "x:b", "telegram:c"]
    o = [obs(r, NEUTRAL, mentions=[t for t in refs if t != r]) for r in refs]
    res = assign(o, cfg)
    assert not any(v.validated for v in res.validations)


def test_deterministe(tmp_path):
    cfg = make_cfg(tmp_path)
    a, b = assign(chaine(), cfg), assign(chaine(), cfg)
    assert a.memberships == b.memberships and a.validations == b.validations


@pytest.mark.parametrize("bad", ["max_iter: 0", "poids_confiance: {semantique: -1}"])
def test_config_scoring_invalide(tmp_path, bad):
    from grappes.config import ConfigError

    (tmp_path / "grappes").mkdir()
    (tmp_path / "scoring.yaml").write_text(bad + "\n", encoding="utf-8")
    (tmp_path / "grappes" / "a.yaml").write_text(
        "id: G\nlexicon: {terms: {a: 1}}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_lien_tme_cite_le_canal_telegram():
    from grappes.ecosystem.crossref import build_citations

    o = obs("x:a", "voir", links=["https://t.me/Canal_B/7", "https://exemple.org/p"])
    assert {c.cited for c in build_citations([o])} == {"telegram:canal_b", "web:exemple.org"}


# --- D15 : conf = S + (100 − S)·(0,6·E + 0,4·A) / 100 -------------------------------------


def _sc(tmp_path, **scoring):
    return make_cfg(tmp_path, **scoring).scoring


@pytest.mark.parametrize(
    ("s", "e", "a", "attendu"),
    [
        (50, 0, 0, 50),  # S seul : conf = S
        (0, 100, 0, 60),  # E seul
        (0, 0, 100, 40),  # A seul
        (50, 100, 100, 100),  # tous
        (50, 50, 50, 75),  # S + 50·(0,6·50 + 0,4·50)/100
        (100, 0, 0, 100),
    ],
)
def test_confiance_complementaire(tmp_path, s, e, a, attendu):
    from grappes.clustering.assign import combine

    assert combine(s, e, a, _sc(tmp_path)) == pytest.approx(attendu)


def test_confiance_sans_citations_vaut_s(tmp_path):
    cfg = make_cfg(tmp_path)
    res = assign([obs("x:solo", STRONG)], cfg)
    m = next(m for m in res.memberships if m["grappe_id"] == "G-A")
    assert m["ecosystem"] == 0 and m["anchor"] == 0
    assert m["confidence"] == m["semantic"] > 50  # plus de plafond à 50


def test_confiance_option_ponderee(tmp_path):
    from grappes.clustering.assign import combine

    sc = _sc(tmp_path, formule_confiance="ponderee")
    assert combine(100, 0, 0, sc) == pytest.approx(50)  # ancienne formule, plafond à 50
    assert combine(100, 100, 100, sc) == pytest.approx(100)
    (tmp_path / "p").mkdir()
    res = assign([obs("x:solo", STRONG)], make_cfg(tmp_path / "p", formule_confiance="ponderee"))
    m = next(m for m in res.memberships if m["grappe_id"] == "G-A")
    assert m["confidence"] == pytest.approx(0.5 * m["semantic"])


@pytest.mark.parametrize(
    "bad", ["formule_confiance: autre", "poids_complementaire: {ecosysteme: 0.8, ancrage: 0.5}"]
)
def test_formule_invalide(tmp_path, bad):
    with pytest.raises(Exception, match="formule_confiance|poids_complementaire"):
        make_cfg(tmp_path, **{bad.split(":", 1)[0]: bad.split(":", 1)[1]})


# --- D19 : E vaut 0 tant que le recoupement n'est pas validé ---------------------------------


def _citants(eco_par_citant):
    obs_list = [obs("web:cible.test", "soupe")]  # S ≈ 28 < seuil_min
    for i, eco in enumerate(eco_par_citant):
        src = f"{eco}:membre{i}"
        link = "https://cible.test/p" if eco != "web" else "https://cible.test/q"
        obs_list.append(obs(src, STRONG))
        obs_list.append(obs(src, NEUTRAL, links=[link]))
    return obs_list


def _membre_a(res, ref):
    eid = next(e.entity_id for e in res.entities if e.primary_ref == ref)
    return next(m for m in res.memberships if m["entity_id"] == eid and m["grappe_id"] == "G-A")


def test_recoupement_refuse_donne_e_nul_et_pas_de_classement(tmp_path):
    res = assign(_citants(["x"] * 5), make_cfg(tmp_path, seuil_min=40))
    m = _membre_a(res, "web:cible.test")
    rec = next(r for r in res.validations if r.cited == "web:cible.test" and r.grappe_id == "G-A")
    assert not rec.validated and "écosystèmes 1 < 2" in " ".join(rec.reasons)
    assert m["ecosystem"] == 0 and m["confidence"] == m["semantic"] > 0
    assert m["status"] == "non_classé" and m["via"] != "recoupement"


def test_second_ecosysteme_valide_e_positif_et_classe(tmp_path):
    res = assign(_citants(["x"] * 3 + ["telegram"] * 2), make_cfg(tmp_path, seuil_min=40))
    m = _membre_a(res, "web:cible.test")
    assert m["ecosystem"] > 0 and m["status"] == "classé" and m["via"] == "recoupement"
    assert m["confidence"] >= 60


def test_explain_signale_le_recoupement_partiel(tmp_path):
    from grappes.mcp.queries import Queries, open_readonly
    from grappes.pipeline import run
    from grappes.storage.db import Database

    cfg = make_cfg(tmp_path)
    db = Database(tmp_path / "d.db")
    db.add_observations(_citants(["x"] * 5), {})
    run(db, cfg)
    q = Queries(open_readonly(tmp_path / "d.db"), cfg)
    eid = next(r["entity_id"] for r in q.search_entities("cible"))
    v = q.explain_assignment(eid, "G-A")["validation"]
    assert v["recoupement_partiel"] and v["e_potentiel"] > 0 and not v["validated"]
    assert any("écosystèmes" in r for r in v["reasons"])
