"""Rapport d'avancement (D70) : inactif par défaut, une ligne JSON par événement, jamais d'exception."""

import json

from grappes import avancement
from grappes.search.base import Journal


def lire(f):
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]


def test_inactif_sans_variable(monkeypatch, tmp_path):
    monkeypatch.delenv(avancement.VARIABLE, raising=False)
    avancement.signaler("etape", etape="recherche")  # ne fait rien, ne lève rien
    assert list(tmp_path.iterdir()) == []


def test_ecrit_une_ligne_json_par_evenement(monkeypatch, tmp_path):
    f = tmp_path / "av.jsonl"
    monkeypatch.setenv(avancement.VARIABLE, str(f))
    avancement.signaler("plan", prises={"tavily": 3})
    avancement.signaler("etape", etape="verification")
    ev = lire(f)
    assert [e["evenement"] for e in ev] == ["plan", "etape"] and ev[0]["prises"] == {"tavily": 3} and "t" in ev[0]


def test_chemin_illisible_ne_leve_pas(monkeypatch):
    monkeypatch.setenv(avancement.VARIABLE, "/nulle/part/absent/av.jsonl")
    avancement.signaler("etape", etape="x")


def test_chaque_appel_reel_du_journal_est_signale_pas_les_refus(monkeypatch, tmp_path):
    f = tmp_path / "av.jsonl"
    monkeypatch.setenv(avancement.VARIABLE, str(f))
    j = Journal(tmp_path / "j.log")
    j.log("tavily", "mot", "GET https://x.test/s", "200")
    j.log("tavily", "mot", "-", "refusé : hors liste blanche")
    j.log("pages", "", "GET https://y.test", "erreur SearchError")
    ev = lire(f)
    assert [(e["prise"], e["statut"]) for e in ev] == [("tavily", "200"), ("pages", "erreur SearchError")]


def test_chaque_resultat_est_rattache_a_la_source_qui_l_a_trouve(monkeypatch, tmp_path):
    from grappes.search.base import SearchContext
    from grappes.search.config import ProviderSpec, SearchConfig
    from grappes.search.engine import run_search
    from grappes.search.http import Response

    f = tmp_path / "av.jsonl"
    monkeypatch.setenv(avancement.VARIABLE, str(f))
    corps = json.dumps({"results": [{"title": "Club Exemple", "url": "https://club-exemple.example/", "content": "x", "id": "1"}]}).encode()
    cfg = SearchConfig(actifs=("fictive",), quota_total=5, delai_s=0.0, journal=tmp_path / "j.log",
                       providers={"fictive": ProviderSpec("fictive", "http", {"url": "https://api.exemple.test/search", "method": "GET", "params": {"q": "{mots_cles}"},
                                                                              "response": {"format": "json", "items": "results"}, "fields": {"title": "title", "url": "url", "description": "content", "id": "id"}})})
    run_search(cfg, ["club"], execute=True, transport=lambda *a, **k: Response(200, {}, corps), sleep=lambda s: None)
    ev = lire(f)
    trouves = [e for e in ev if e["evenement"] == "trouve"]
    assert trouves == [{"t": trouves[0]["t"], "evenement": "trouve", "prise": "fictive", "cle": "web:club-exemple.example"}]
