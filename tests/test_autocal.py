"""Mission autocalibration (D62) : cassette réseau, jugement en trois comptes, métriques trec, découverte de vérité.
Tout est hors ligne : Wikidata est remplacé par un faux client."""

import pytest

from grappes.autocal import td
from grappes.autocal.cassette import Cassette, Recorder, Replayer
from grappes.autocal.juger import FAUX, JUSTE, NON_VERIFIABLE, Juge, norm_name
from grappes.autocal.metriques import evaluer
from grappes.autocal.reference import RefEntity, Reference
from grappes.search.base import SearchError
from grappes.search.http import Response


def _inner(calls):
    def fake(method, url, headers, body, timeout, max_bytes):
        calls.append(url)
        if "boom" in url:
            raise SearchError("erreur réseau : TimeoutError")
        return Response(200, {"Content-Type": "text/html"}, b"<html>ok</html>")

    return fake


def test_cassette_enregistre_puis_rejoue_sans_reseau(tmp_path):
    calls: list[str] = []
    cass = Cassette(tmp_path / "c.sqlite")
    rec = Recorder(cass, inner=_inner(calls))
    r1 = rec("GET", "https://a.example/x", {"Authorization": "Bearer SECRET"}, None, 5, 1000)
    assert r1.body == b"<html>ok</html>" and len(calls) == 1
    rec("GET", "https://a.example/x", {}, None, 5, 1000)  # deuxième fois : servie par la cassette
    assert len(calls) == 1
    with pytest.raises(SearchError):
        rec("GET", "https://boom.example/", {}, None, 5, 1000)
    rep = Replayer(cass)
    assert rep("GET", "https://a.example/x", {}, None, 5, 1000).status == 200
    with pytest.raises(SearchError):  # rejouée : l'échec enregistré revient
        rep("GET", "https://boom.example/", {}, None, 5, 1000)
    with pytest.raises(SearchError):  # absente : erreur franche, jamais de réseau
        rep("GET", "https://autre.example/", {}, None, 5, 1000)
    assert rep.misses == ["https://autre.example/"]
    # la clé d'API (en-têtes) n'est jamais écrite en base
    assert b"SECRET" not in (tmp_path / "c.sqlite").read_bytes()


def test_cassette_distingue_les_corps(tmp_path):
    calls: list[str] = []
    rec = Recorder(Cassette(tmp_path / "c.sqlite"), inner=_inner(calls))
    rec("POST", "https://api.example/s", {}, b'{"q": "a"}', 5, 1000)
    rec("POST", "https://api.example/s", {}, b'{"q": "b"}', 5, 1000)
    assert len(calls) == 2


class FauxWikidata:
    """Faux client : un seul élément connu par libellé, avec ses classes."""

    def __init__(self, items):
        self.items = items  # libellé -> (qid, [classes P31])

    def search(self, text, limit=5, language="fr"):
        return [
            {"id": q, "label": lab}
            for lab, (q, _c) in self.items.items()
            if norm_name(lab) == norm_name(text)
        ]

    def api(self, **p):
        out = {}
        for lab, (q, classes) in self.items.items():
            if q in p["ids"].split("|"):
                out[q] = {
                    "claims": {
                        "P31": [{"mainsnak": {"datavalue": {"value": {"id": c}}}} for c in classes]
                    }
                }
        return {"entities": out}

    def sparql(self, query):
        # sous-classes : Q_CLUB_PRO est une sous-classe de Q_CLUB
        return [{"c": "Q_CLUB_PRO"}] if "Q_CLUB_PRO" in query else []


def _juge(tmp_path):
    ref = Reference(
        "s",
        "phrase",
        "",
        [RefEntity("Q1", "Montpellier Handball", ["MHB"], ["montpellierhandball.com"])],
        ["Q_CLUB"],
    )
    w = FauxWikidata(
        {
            "L'Équipe": ("Q2", ["Q_JOURNAL"]),
            "Club inconnu": ("Q3", ["Q_CLUB_PRO"]),
        }
    )
    return Juge(ref, w, tmp_path / "juge.json")


def test_trois_comptes(tmp_path):
    j = _juge(tmp_path)
    assert (
        j.juger("n'importe quoi", "web:www.montpellierhandball.com").verdict == JUSTE
    )  # domaine officiel
    assert j.juger("mhb", "web:autre.fr").verdict == JUSTE  # alias exact
    v = j.juger("L'Équipe", "web:lequipe.fr")
    assert v.verdict == FAUX and v.docid == "Q2"  # libellé exact, mauvaise classe
    v = j.juger("Club inconnu", "web:x.fr")
    assert v.verdict == NON_VERIFIABLE  # bonne classe mais hors référence : jamais juste
    assert j.juger("Rien du tout", "web:y.fr").verdict == NON_VERIFIABLE  # absent : pas un faux


def test_metriques_trec_non_verifiable_jamais_juste():
    qrels = {"s": {"A": 1, "B": 1, "C": 1, "F": 0}}
    run = {"s": {"A": 0.9, "x:inconnu": 0.8, "B": 0.7, "F": 0.6}}
    m = evaluer(qrels, run)["s"]
    assert m["Rprec"] == pytest.approx(
        2 / 3
    )  # R=3 : A et B justes, x:inconnu non jugé donc non pertinent (sinon 1)
    assert 0 <= m["bpref"] <= 1 and 0 <= m["infAP"] <= 1


def test_decouverte_de_verite_classe_les_plus_citees():
    cits = [
        ("web:a.fr", "web:x.fr"),
        ("web:b.fr", "web:x.fr"),
        ("web:c.fr", "web:x.fr"),
        ("web:a.fr", "web:y.fr"),
    ]
    for algo in ("vote", "sums", "averagelog", "investment", "pooledinvestment", "truthfinder"):
        b = td.croyances(cits, algo, {"web:a.fr": 0.5, "web:b.fr": 0.5, "web:c.fr": 0.5})
        assert b["web:x.fr"] >= b["web:y.fr"], algo
    assert td.croyances([], "sums") == {}
    # variante avec négatifs : une source lue qui ne cite pas affirme 0
    b = td.croyances(
        cits, "vote", None, lues={"web:a.fr", "web:b.fr", "web:c.fr", "web:d.fr"}, negatifs=True
    )
    assert 0 < b["web:y.fr"] < b["web:x.fr"] <= 1


def test_calibration_et_permutation():
    import numpy as np

    from grappes.autocal import controles as c

    rng = np.random.default_rng(0)
    s = rng.random(2000)
    y = (rng.random(2000) < s).astype(int)  # score bien calibré par construction
    for m in ("platt", "isotone"):
        p = c.ajuster(s, y, m).proba(s)
        assert c.ece(p, y) < 0.05 and c.brier(p, y) <= c.brier_base(y)
    assert c.ece(np.full(2000, 0.9), y) > 0.3  # un score trop confiant est vu comme tel
    docids = [f"d{i}" for i in range(30)]
    justes = set(docids[:10])  # classement parfait : les 10 justes en tête, R = 10
    r = c.permutation("s", docids, justes, 10, n_perm=200)
    assert r["observe"] == 1.0 and r["permute_moyen"] < 0.6 and r["p_valeur"] < 0.05
    # classement sans lien avec la pertinence : indiscernable du hasard
    hasard = set(docids[i] for i in range(0, 30, 3))
    r = c.permutation("s", docids, hasard, 10, n_perm=200)
    assert r["p_valeur"] > 0.05
