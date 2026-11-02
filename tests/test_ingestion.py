import json
from pathlib import Path

import pytest

from grappes.ingestion import FetcherError, get_fetcher, registered_ecosystems
from grappes.ingestion.base import BaseFetcher, register
from grappes.models import SourceRef

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return [json.loads(line) for line in (FIX / f"{name}.jsonl").read_text("utf-8").splitlines()]


def test_registre():
    assert registered_ecosystems() == ["instagram", "telegram", "web", "x"]
    with pytest.raises(FetcherError):
        get_fetcher("inconnu")


def test_registre_refuse_doublon_et_classe_sans_ecosysteme():
    class Sans(BaseFetcher):
        def parse(self, raw):
            return []

    with pytest.raises(ValueError):
        register(Sans)

    class Dup(BaseFetcher):
        ecosystem = "x"

        def parse(self, raw):
            return []

    with pytest.raises(ValueError):
        register(Dup)


def test_fetch_raw_hors_perimetre():
    with pytest.raises(NotImplementedError):
        get_fetcher("x").fetch_raw()


# --- X -----------------------------------------------------------------------------------


def test_x_nominal():
    obs = next(iter(get_fetcher("x").parse(load("x")[0])))
    assert obs.source == SourceRef("x", "chef_demo")
    assert obs.source.key == "x:chef_demo"
    assert obs.mentions == (SourceRef("x", "jardin_demo"),)
    assert obs.links == ("https://exemple-a.test/soupe",)  # expanded_url nettoyée
    assert obs.hashtags == ("cuisine",)
    assert obs.engagement == 14
    assert obs.ts is not None and obs.ts.year == 2026
    assert obs.meta == {"lang": "fr"}


def test_x_champs_manquants_et_forme_alternative():
    obs = next(iter(get_fetcher("x").parse(load("x")[1])))
    assert obs.source.ident == "jardin_demo"
    assert obs.hashtags == ("jardinage",)  # extrait du texte
    assert obs.ts is not None  # epoch
    assert obs.engagement == 0 and obs.links == () and obs.mentions == ()


def test_x_encodage_non_latin_preserve():
    obs = next(iter(get_fetcher("x").parse(load("x")[2])))
    assert "Привет" in obs.text and "Բարեւ" in obs.text and obs.ts is None


def test_x_rejets_et_doublons():
    obs, rep = get_fetcher("x").parse_many(load("x"))
    assert rep.accepted == 4 and rep.duplicates == 1
    assert rep.n_rejected == 1 and rep.rejected[0][0] == 4  # sans auteur
    assert len({o.obs_id for o in obs}) == 4
    # id 1005 : texte null -> texte vide accepté, date illisible -> ts None
    assert any(o.raw_ref == "1005" and o.text == "" and o.ts is None for o in obs)


def test_x_determinisme_obs_id():
    f = get_fetcher("x")
    a = next(iter(f.parse(load("x")[0]))).obs_id
    b = next(iter(f.parse(load("x")[0]))).obs_id
    assert a == b


# --- Telegram ----------------------------------------------------------------------------


def test_telegram_nominal():
    obs = next(iter(get_fetcher("telegram").parse(load("telegram")[0])))
    assert obs.source.key == "telegram:canal_cuisine"
    keys = {m.key for m in obs.mentions}
    assert keys == {"telegram:chef_demo", "telegram:canal_jardin"}
    assert obs.links == ("https://exemple-b.test/page",)
    assert obs.hashtags == ("cuisine",)
    assert obs.engagement == 125
    assert obs.raw_ref == "canal_cuisine/7"


def test_telegram_epoch_et_sans_arobase():
    obs = next(iter(get_fetcher("telegram").parse(load("telegram")[1])))
    assert obs.source.ident == "canal_jardin" and obs.ts is not None


def test_telegram_rejets_et_doublons():
    obs, rep = get_fetcher("telegram").parse_many(load("telegram"))
    assert len(obs) == 2 and rep.duplicates == 1
    assert rep.n_rejected == 2


# --- Web ---------------------------------------------------------------------------------


def test_web_nominal():
    obs = next(iter(get_fetcher("web").parse(load("web")[0])))
    assert obs.source.key == "web:exemple-a.test"
    assert obs.text.startswith("Soupe d'hiver")
    # liens sortants : canonicalisés, liens internes exclus
    assert obs.links == ("https://exemple-b.test/page", "https://t.me/canal_cuisine")
    assert obs.meta["page_url"] == "https://exemple-a.test/soupe"


def test_web_champs_manquants():
    obs = next(iter(get_fetcher("web").parse(load("web")[1])))
    assert obs.source.ident == "exemple-c.test" and obs.ts is None and obs.links == ()


def test_web_rejets():
    obs, rep = get_fetcher("web").parse_many(load("web"))
    assert len(obs) == 2 and rep.n_rejected == 2


def test_parse_many_tolere_non_objets():
    obs, rep = get_fetcher("x").parse_many([42, None, "x", {"id": 1, "author": "a", "text": "t"}])
    assert len(obs) == 1 and rep.n_rejected == 3
