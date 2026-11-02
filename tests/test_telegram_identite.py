"""D25 : identité des canaux Telegram — table d'équivalence, hypothèse, homonymes, accents."""

import json
from dataclasses import replace

import pytest

from conftest import CONFIG_CLAIR
from grappes.clustering.assign import assign
from grappes.config import ConfigError, load_config
from grappes.ecosystem.crossref import build_citations
from grappes.ingestion import get_fetcher
from grappes.mcp.queries import Queries, open_readonly
from grappes.models import Observation, SourceRef
from grappes.pipeline import ingest, run
from grappes.storage.db import Database
from test_phase4 import STRONG, make_cfg, obs


def export(name, cid, messages=None, **extra):
    msgs = messages or [
        {"id": 1, "type": "message", "date_unixtime": "1788340500", "text": "Bonjour", "from": name}
    ]
    return {"name": name, "type": "public_channel", "id": cid, "messages": msgs, **extra}


def parse(doc, names=None):
    class Cfg:
        telegram_names = names or {}

    f = get_fetcher("telegram").configure(Cfg())
    obs_, rep = f.parse_many([r for r in f.unwrap(doc)])
    return obs_, rep


# --- nom présent / absent / accents -----------------------------------------------------------


def test_nom_present_dans_la_table_est_exact():
    obs_, _ = parse(export("Canal Démo A", 1), {"canal demo a": "canal_demo_a"})
    o = obs_[0]
    assert o.source.key == "telegram:canal_demo_a" and o.hypothetical == ()
    assert o.meta == {"nom": "Canal Démo A", "chat_id": "1"}


def test_nom_absent_derive_et_marque_hypothese():
    o = parse(export("Canal Jardin", 2))[0][0]
    assert o.source.key == "telegram:canal_jardin"
    assert o.hypothetical == ("telegram:canal_jardin",)


def test_accents_et_casse_de_la_derivation_et_de_la_table():
    assert parse(export("Café Crème Info", 3))[0][0].source.key == "telegram:cafe_creme_info"
    exact = parse(export("CAFE   CREME-info", 4), {"cafe creme info": "cafe_creme"})[0][0]
    assert exact.source.key == "telegram:cafe_creme" and exact.hypothetical == ()  # clé normalisée
    arm = parse(export("Բարի լույս", 5))[0][0]  # écriture non latine : conservée, jamais exacte
    assert arm.hypothetical and arm.source.ident == "բարի_լույս"


def test_nom_public_de_l_export_est_exact():
    o = parse(export("Canal Quelconque", 6, username="Canal_Public"))[0][0]
    assert o.source.key == "telegram:canal_public" and o.hypothetical == ()


def test_transfert_par_nom_affiche_hypothese_sauf_table_et_mention_exacte():
    msg = [
        {"id": 1, "type": "message", "text": "x @canal_b", "forwarded_from": "Canal B"},
        {"id": 2, "type": "message", "text": "y", "forwarded_from": "Canal Autre"},
        {"id": 3, "type": "message", "text": "z", "forwarded_from": "@canal_pub"},
    ]
    o1, o2, o3 = parse(export("Source", 7, msg), {"source": "source_pub"})[0]
    assert (
        o1.hypothetical == ()
    )  # « Canal B » dérivé = canal_b, mais aussi cité en exact (@canal_b)
    assert o2.hypothetical == ("telegram:canal_autre",)
    assert o3.hypothetical == () and "telegram:canal_pub" in {m.key for m in o3.mentions}


def test_table_incoherente_refusee(tmp_path):
    cfg = tmp_path / "config"
    (cfg / "grappes").mkdir(parents=True)
    (cfg / "grappes" / "g.yaml").write_text("id: G\nlexicon: {terms: {a: 1}}\n")
    (cfg / "telegram.yaml").write_text(
        'equivalences: {"Canal A": canal_a, "canal  à": canal_autre}\n'
    )
    with pytest.raises(ConfigError, match="deux identifiants"):
        load_config(cfg)


def test_table_chargee_depuis_la_configuration(tmp_path):
    cfg = tmp_path / "config"
    (cfg / "grappes").mkdir(parents=True)
    (cfg / "grappes" / "g.yaml").write_text("id: G\nlexicon: {terms: {a: 1}}\n")
    (cfg / "telegram.yaml").write_text('equivalences: {"Canal Démo A": "@Canal_Demo_A"}\n')
    assert load_config(cfg).telegram_names == {"canal demo a": "canal_demo_a"}


# --- homonymes : ne rien fusionner -----------------------------------------------------------


def test_deux_canaux_de_meme_nom_ne_sont_pas_fusionnes(tmp_path):
    cfg = load_config(CONFIG_CLAIR)
    db = Database(tmp_path / "t.db")
    for n, (cid, texte) in enumerate([(100, "premier"), (200, "second")]):
        d = tmp_path / f"in{n}"
        d.mkdir()
        doc = export("Canal Jardin", cid, [{"id": 1, "type": "message", "text": texte}])
        (d / "t.json").write_text(json.dumps(doc), encoding="utf-8")
        ingest(db, d, "telegram", cfg)
    refs = {o.source.key for o in db.load_observations()}
    assert refs == {"telegram:canal_jardin", "telegram:canal_jardin~200"}
    assert db.native_ids() == {"telegram:canal_jardin": "100", "telegram:canal_jardin~200": "200"}
    d = tmp_path / "in1"  # ré-ingestion du second : idempotente, toujours distinct
    assert ingest(db, d, "telegram", cfg)[0] == 0
    _, res = run(db, cfg)
    assert len({e.entity_id for e in res.entities}) == 2


# --- une hypothèse ne valide jamais un recoupement -------------------------------------------


def _scene(tmp_path, hypothetical_tg: bool):
    """Deux membres X et deux membres Telegram citent une même cible ; l'identité des
    Telegram est exacte ou seulement une hypothèse."""
    tmp_path.mkdir(exist_ok=True)
    cfg = make_cfg(tmp_path, seuil_min=30)
    cible = "https://cible.test/p"
    obs_list = [obs("web:cible.test", "soupe")]
    for i in range(2):
        obs_list.append(obs(f"x:membre{i}", STRONG))
        obs_list.append(obs(f"x:membre{i}", "neutre", links=[cible]))
    for i in range(2):
        src = f"telegram:tg{i}"
        for o in (obs(src, STRONG), obs(src, "neutre", links=[cible])):
            obs_list.append(replace(o, hypothetical=(src,)) if hypothetical_tg else o)
    return assign(obs_list, cfg)


def _rec(res, ref="web:cible.test"):
    return next(r for r in res.validations if r.cited == ref and r.grappe_id == "G-A")


def test_identite_exacte_valide_le_recoupement(tmp_path):
    res = _scene(tmp_path / "exact", hypothetical_tg=False)
    assert _rec(res).validated and set(_rec(res).ecosystems) == {"x", "telegram"}


def test_identite_hypothetique_ne_valide_pas(tmp_path):
    res = _scene(tmp_path / "hypo", hypothetical_tg=True)
    rec = _rec(res)
    assert not rec.validated and set(rec.ecosystems) == {
        "x"
    }  # les citants hypothétiques ne comptent pas
    assert sum(c.hypothesis for c in res.citations) == 2  # mais leurs citations sont conservées


def test_explain_signale_les_citations_hypothetiques(tmp_path):
    cfg = make_cfg(tmp_path, seuil_min=30)
    src = SourceRef("telegram", "canal_h")
    db = Database(tmp_path / "e.db")
    db.add_observations(
        [
            Observation("o1", SourceRef("web", "cible.test"), None, "soupe"),
            Observation(
                "o2", src, None, STRONG, links=("https://cible.test/p",), hypothetical=(src.key,)
            ),
        ],
        {},
    )
    run(db, cfg)
    db.close()
    q = Queries(open_readonly(tmp_path / "e.db"), cfg)
    eid = next(r["entity_id"] for r in q.search_entities("cible"))
    fiche = q.explain_assignment(eid, "G-A")
    assert fiche["citations_hypothetiques"] == ["telegram:canal_h"]
    assert build_citations(
        [Observation("o", src, None, "t", links=("https://cible.test/",), hypothetical=(src.key,))]
    )[0].hypothesis
