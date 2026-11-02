"""Échantillons de formats fournis par Sednal (REPONSES_SEDNAL_3, Q2) : valeurs inventées,
construits d'après les documentations officielles. Ce ne sont pas des exports réels."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from grappes.config import load_config
from grappes.ecosystem.crossref import build_citations
from grappes.ingestion import get_fetcher
from grappes.pipeline import ingest, run
from grappes.storage.db import Database

ECH = Path(__file__).parent / "fixtures" / "echantillons"
CONFIG = Path(__file__).resolve().parents[1] / "config"


def lire(eco: str, nom: str):
    fetcher = get_fetcher(eco)
    doc = json.loads((ECH / nom).read_text("utf-8"))
    docs = doc if isinstance(doc, list) else [doc]  # comme `read_json_records`
    records = [r for d in docs for r in fetcher.unwrap(d)]
    return fetcher.parse_many(records)


def refs(obs):
    return {o.obs_id: o for o in obs}


# --- X (API v2) ------------------------------------------------------------------------------


def test_x_auteurs_resolus_par_includes_users():
    obs, rep = lire("x", "x_1.json")
    assert rep.accepted == 3 and rep.n_rejected == 0
    assert [o.source.key for o in obs] == ["x:compte_demo_a", "x:compte_demo_b", "x:compte_demo_c"]


def test_x_entites_metriques_et_date():
    a, b, c = lire("x", "x_1.json")[0]
    assert [m.key for m in a.mentions] == ["x:compte_demo_b"]  # `username`, pas `handle`
    assert a.links == ("https://exemple-a.example.test/articles/jardin-1",)  # expanded_url
    assert a.hashtags == ("jardinage",)  # `{"tag": ...}`
    assert a.engagement == 12 + 3 + 1  # like + retweet + reply (quote, bookmark, impression exclus)
    assert a.ts == datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    assert a.meta == {"lang": "fr", "nom": "Compte Demo A"}
    assert [m.key for m in b.mentions] == ["x:compte_demo_a"]  # « RT @... »
    assert c.mentions == () and c.links == () and c.engagement == 0


def test_x_auteur_introuvable_sans_includes():
    doc = json.loads((ECH / "x_1.json").read_text("utf-8"))
    del doc["includes"]
    fetcher = get_fetcher("x")
    obs, rep = fetcher.parse_many(fetcher.unwrap(doc))
    assert obs == [] and rep.n_rejected == 3
    assert "author_id absent de includes.users" in rep.rejected[0][1]


# --- Telegram (export Desktop) ----------------------------------------------------------------


def test_telegram_messages_et_service_ignore():
    obs, rep = lire("telegram", "telegram_1.json")
    assert rep.accepted == 3 and rep.ignored == 1 and rep.n_rejected == 0  # `service` ignoré
    assert {o.source.key for o in obs} == {"telegram:canal_demo_a"}


def test_telegram_texte_liste_entites_et_date_unixtime():
    m2, _, m4 = lire("telegram", "telegram_1.json")[0]
    assert m2.text.startswith("Nouvel article sur le jardinage : https://exemple-a")
    assert "@compte_demo_a" in m2.text and m2.hashtags == ("jardinage",)
    assert m2.links == ("https://exemple-a.example.test/articles/jardin-1",)
    assert [m.key for m in m2.mentions] == ["telegram:compte_demo_a"]
    assert m2.ts == datetime.fromtimestamp(1788340500, tz=UTC)  # `date_unixtime` en chaîne
    assert m4.engagement == 0


def test_telegram_transfert_invitation_et_lien_de_canal():
    m3 = lire("telegram", "telegram_1.json")[0][1]
    assert "telegram:canal_demo_b" in {m.key for m in m3.mentions}  # forwarded_from (nom affiché)
    assert "https://t.me/+AbCdEf0123456" in m3.links  # conservé en observation...
    cites = {c.cited for c in build_citations([m3])}
    assert cites == {"telegram:canal_demo_b"}  # ... mais jamais cité (invitation, D24)


def test_telegram_ecritures_mixtes_conservees():
    m4 = lire("telegram", "telegram_1.json")[0][2]
    assert "Բարի" in m4.text and "друзья" in m4.text


# --- Instagram (API Graph) --------------------------------------------------------------------


def test_instagram_medias():
    obs, rep = lire("instagram", "instagram_1.json")
    assert rep.accepted == 3 and rep.n_rejected == 0
    m1, m2, m3 = obs
    assert m1.source.key == "instagram:compte_demo_c"
    assert [m.key for m in m1.mentions] == ["instagram:compte_demo_a"]
    assert m1.hashtags == ("jardinage",)
    assert m1.links == ("https://exemple-a.example.test/articles/jardin-1",)
    assert m1.engagement == 45 + 4
    assert m1.ts == datetime(2026, 9, 2, 11, 0, tzinfo=UTC)  # `+0000` sans deux-points
    assert m2.text == "" and m2.meta == {"media_type": "CAROUSEL_ALBUM"}  # sans légende
    assert "Տեսանյութ" in m3.text and "видео" in m3.text


def test_instagram_username_manquant_rejete():
    obs, rep = get_fetcher("instagram").parse_many([{"id": "1", "caption": "x"}])
    assert obs == [] and rep.rejected == [(0, "username manquant")]


# --- Web (format proposé) ---------------------------------------------------------------------


def test_web_pages_liens_href_et_dates():
    obs, rep = lire("web", "web_1.json")
    assert rep.accepted == 2
    a, b = obs
    assert a.source.key == "web:exemple-a.example.test"  # canonical_url sans utm
    assert a.ts == datetime(2026, 9, 2, 7, 0, tzinfo=UTC)  # published_at avant fetched_at
    assert b.ts == datetime(2026, 9, 5, 6, 5, tzinfo=UTC)  # repli sur fetched_at
    assert a.meta["lang"] == "fr"
    cites = {c.cited for c in build_citations([a, b])}
    assert cites == {
        "telegram:canal_demo_a",
        "x:compte_demo_a",
        "web:exemple-b.example.test",  # citée par a
        "web:exemple-a.example.test",  # citée par b
        "instagram:compte_demo_c",
    }


# --- bout en bout : recoupement entre écosystèmes -----------------------------------------


@pytest.fixture
def base_echantillons(tmp_path):
    cfg = load_config(CONFIG)
    db = Database(tmp_path / "e.db")
    for eco, nom in (
        ("x", "x_1.json"),
        ("telegram", "telegram_1.json"),
        ("instagram", "instagram_1.json"),
        ("web", "web_1.json"),
    ):
        dossier = tmp_path / eco
        dossier.mkdir()
        (dossier / nom).write_bytes((ECH / nom).read_bytes())
        new, rep = ingest(db, dossier, eco, cfg)
        assert new > 0 and rep.n_rejected == 0, eco
    return cfg, db


def test_pipeline_complet_sur_les_echantillons(base_echantillons):
    cfg, db = base_echantillons
    _, res = run(db, cfg)
    cles = {e.primary_ref for e in res.entities}
    assert {
        "x:compte_demo_a", "x:compte_demo_b", "x:compte_demo_c",
        "telegram:canal_demo_a", "telegram:canal_demo_b", "telegram:compte_demo_a",
        "instagram:compte_demo_c", "instagram:compte_demo_a",
        "web:exemple-a.example.test", "web:exemple-b.example.test",
    } <= cles  # fmt: skip
    # aucune entité fantôme issue des plateformes (liens non identifiables, D24)
    assert not cles & {"web:t.me", "web:x.com", "web:instagram.com", "web:t.co"}
    types = {e.primary_ref: e.type.value for e in res.entities}
    assert (
        types["instagram:compte_demo_c"] == "person" and types["telegram:canal_demo_a"] == "group"
    )
    assert {c.citer.split(":")[0] for c in res.citations} == {"x", "telegram", "instagram", "web"}


def test_comptes_homonymes_de_plusieurs_ecosystemes_restent_distincts(base_echantillons):
    """Jamais de ré-identification : `x:compte_demo_a` et `telegram:compte_demo_a` sont deux
    entités, même si le nom est identique."""
    cfg, db = base_echantillons
    _, res = run(db, cfg)
    cles = [e.primary_ref for e in res.entities]
    assert "x:compte_demo_a" in cles and "telegram:compte_demo_a" in cles
    assert len({e.entity_id for e in res.entities}) == len(cles)
