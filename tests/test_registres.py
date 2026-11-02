"""Registres officiels (A) et classement Tranco (affinage de D) : transport factice, aucun réseau."""

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from grappes.fiabilite import Reliability, load_fiabilite
from grappes.registres import load_registres, matched_info, matched_refs, update
from grappes.search.base import Journal
from grappes.search.http import Response
from grappes.storage.db import Database
from grappes.tranco import candidates, ensure, ranks_for

ROOT = Path(__file__).resolve().parents[1]
TABLE = load_fiabilite(ROOT / "config" / "fiabilite_sources.yaml")
REG = load_registres(ROOT / "config" / "registres.yaml")
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


class Fake:
    """Transport factice : enregistre les URL demandées."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append(url)
        for frag, (status, payload) in self.routes.items():
            if frag in url:
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return Response(status, {}, data)
        return Response(404, {}, b"{}")


def add_source(db, ref, name=None):
    eco, ident = ref.split(":", 1)
    with db.transaction() as c:
        c.execute(
            "INSERT INTO sources(ref_key, ecosystem, ident, weight, display_name) VALUES (?,?,?,1,?)",
            (ref, eco, ident, name),
        )


def sirene(*results):
    return {"results": list(results)}


def org(nom, etat="A", siren="123456789"):
    return {"nom_complet": nom, "etat_administratif": etat, "siren": siren, "complements": {}}


ESR = {"results": [{"url": "https://www.univ-exemple.fr/accueil"}]}


def setup(tmp_path):
    return Database(tmp_path / "t.db"), Journal(None)


def test_domaine_officiel_place_en_a(tmp_path):
    db, j = setup(tmp_path)
    add_source(db, "web:univ-exemple.fr")
    add_source(db, "web:blog.univ-exemple.fr")
    add_source(db, "web:autre.com")
    t = Fake({"enseignementsup": (200, ESR), "recherche-entreprises": (200, sirene())})
    update(db, REG, NOW, t, j)
    assert matched_refs(db, REG) == {"web:univ-exemple.fr", "web:blog.univ-exemple.fr"}


def test_nom_exact_actif_unique(tmp_path):
    db, j = setup(tmp_path)
    add_source(db, "web:asso.org", "Association Les Jardins Unis")
    t = Fake({"recherche-entreprises": (200, sirene(org("ASSOCIATION LES JARDINS UNIS")))})
    update(db, REG, NOW, t, j)
    assert matched_refs(db, REG) == {"web:asso.org"}


def test_homonymes_organisme_ferme_ou_nom_voisin_jamais_a(tmp_path):
    db, j = setup(tmp_path)
    for i, nom in enumerate(["Jardins Unis", "Potagers Unis", "Semis Unis"]):
        add_source(db, f"web:s{i}.org", nom)
    rep = {
        "Jardins+Unis": sirene(org("JARDINS UNIS", siren="1"), org("Jardins Unis", siren="2")),
        "Potagers+Unis": sirene(org("POTAGERS UNIS", etat="C")),
        "Semis+Unis": sirene(org("SEMIS UNIS ET ASSOCIES")),
    }
    t = Fake({"enseignementsup": (200, {"results": []})} | {k: (200, v) for k, v in rep.items()})
    update(db, REG, NOW, t, j)
    assert matched_refs(db, REG) == frozenset()


def test_personnes_et_wikipedia_jamais_interroges(tmp_path):
    db, j = setup(tmp_path)
    add_source(db, "x:jean_dupont", "Jean Dupont")
    add_source(db, "telegram:canal", "Canal Jardin")
    add_source(db, "wikipedia:Jardin_partage", "Jardin partagé")
    t = Fake({})
    update(db, REG, NOW, t, j)
    assert not any("recherche-entreprises" in u for u in t.calls)


def test_cache_et_quota(tmp_path):
    db, j = setup(tmp_path)
    for i in range(5):
        add_source(db, f"web:s{i}.org", f"Organisme {i}")
    t = Fake({"enseignementsup": (200, ESR), "recherche-entreprises": (200, sirene())})
    small = dataclasses.replace(REG, max_requetes=3)
    s = update(db, small, NOW, t, j)
    assert s["requetes"] == 3 and len(t.calls) == 3  # plafond du cycle
    t2 = Fake({"enseignementsup": (200, ESR), "recherche-entreprises": (200, sirene())})
    update(db, REG, NOW + timedelta(days=1), t2, j)
    # le cache (30 j) évite de réinterroger l'ESR et les deux noms déjà vus
    assert not any("enseignementsup" in u for u in t2.calls)
    assert len(t2.calls) == 3 and not any("Organisme+0" in u for u in t2.calls)
    t3 = Fake({"enseignementsup": (200, ESR), "recherche-entreprises": (200, sirene())})
    update(db, REG, NOW + timedelta(days=40), t3, j)
    assert any("enseignementsup" in u for u in t3.calls)  # cache périmé : renouvelé


def test_registre_en_panne_ne_suppose_rien(tmp_path):
    db, j = setup(tmp_path)
    add_source(db, "web:asso.org", "Association X")
    update(
        db, REG, NOW, Fake({"enseignementsup": (500, {}), "recherche-entreprises": (503, {})}), j
    )
    assert matched_refs(db, REG) == frozenset()


def test_a_pese_plus_que_d_et_change_l_empreinte():
    base = Reliability(TABLE)
    a = Reliability(TABLE, frozenset({"web:univ-exemple.fr"}))
    assert a.letter("web:univ-exemple.fr") == "A" and base.letter("web:univ-exemple.fr") == "D"
    assert a.factor("web:univ-exemple.fr") > 3 * base.factor("web:univ-exemple.fr")
    assert a.fingerprint != base.fingerprint


# --- Tranco -------------------------------------------------------------------------------------

LISTE = b"1,google.com\n2,facebook.com\n500,lemonde.fr\n90000,exemple.org\n"


def tranco_net():
    return Fake(
        {
            "/api/lists/date/": (200, {"list_id": "ABCD", "created_on": "2026-10-10", "available": True, "download": "https://tranco-list.eu/download/ABCD/1000000"}),
            "/download/ABCD": (200, LISTE),
        }
    )  # fmt: skip


def test_telechargement_unique_puis_cache(tmp_path):
    t = tranco_net()
    meta = ensure(tmp_path / "tr", NOW, t, Journal(None))
    assert meta["liste_id"] == "ABCD" and len(t.calls) == 2
    t2 = tranco_net()
    assert ensure(tmp_path / "tr", NOW + timedelta(days=5), t2, Journal(None)) == meta
    assert t2.calls == []  # jamais réinterrogé tant que le cache est frais
    t3 = tranco_net()
    ensure(tmp_path / "tr", NOW + timedelta(days=45), t3, Journal(None))
    assert len(t3.calls) == 2  # renouvelé après cache_jours


def test_echec_garde_l_ancien_cache(tmp_path):
    ensure(tmp_path / "tr", NOW, tranco_net(), Journal(None))
    meta = ensure(tmp_path / "tr", NOW + timedelta(days=60), Fake({}), Journal(None))
    assert meta and meta["liste_id"] == "ABCD"
    assert ensure(tmp_path / "vide", NOW, Fake({}), Journal(None)) is None


def test_rangs_et_sous_domaines(tmp_path):
    ensure(tmp_path / "tr", NOW, tranco_net(), Journal(None))
    assert candidates("blog.exemple.org") == ["blog.exemple.org", "exemple.org"]
    r = ranks_for(tmp_path / "tr", {"blog.exemple.org", "lemonde.fr", "inconnu.net"})
    assert r == {"blog.exemple.org": 90000, "lemonde.fr": 500}


def test_tranco_affine_d_sans_jamais_depasser_c_ni_b():
    ranks = {"exemple.org": 90000, "gros.com": 1, "petit.net": 999_999}
    r = Reliability(TABLE, tranco=ranks, tranco_id="ABCD@2026-10-10")
    d, c, b = TABLE.factors["D"], TABLE.factors["C"], TABLE.factors["B"]
    assert d < r.factor("web:exemple.org") < c
    assert r.factor("web:gros.com") <= c < b
    assert r.factor("web:petit.net") < r.factor("web:exemple.org") < r.factor("web:gros.com")
    assert r.factor("web:absent.io") == d  # hors classement : D inchangé
    assert r.factor("wikipedia:Jardin") == c  # Tranco ne touche que web/rss
    assert r.fingerprint != Reliability(TABLE).fingerprint
    assert r.letter("web:gros.com") == "D"  # la lettre ne change pas, seul le poids est affiné


def test_ville_et_pays_viennent_du_registre(tmp_path):
    db, j = setup(tmp_path)
    add_source(db, "web:univ-exemple.fr")
    add_source(db, "web:asso.org", "Association Les Jardins Unis")
    esr = {"results": [{"url": "https://www.univ-exemple.fr/", "com_nom": "Avignon"}]}
    siege = org("ASSOCIATION LES JARDINS UNIS") | {"siege": {"libelle_commune": "MONTLUCON"}}
    t = Fake({"enseignementsup": (200, esr), "recherche-entreprises": (200, sirene(siege))})
    update(db, REG, NOW, t, j)
    assert matched_info(db, REG) == {
        "web:univ-exemple.fr": ("Avignon", "FR"),
        "web:asso.org": ("MONTLUCON", "FR"),
    }


def test_ancien_cache_sans_ville_vide_une_fois(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE registre_domaines (registre TEXT NOT NULL, domaine TEXT NOT NULL, maj TEXT NOT NULL, PRIMARY KEY (registre, domaine))"
    )
    con.execute("INSERT INTO registre_domaines VALUES ('r','x.fr','2026-10-01')")
    con.commit()
    con.close()
    db = Database(path)  # migration : colonne ajoutée, ancien cache vidé pour être rechargé
    assert db.conn.execute("SELECT COUNT(*) FROM registre_domaines").fetchone()[0] == 0
    db.close()
    db = Database(path)  # deuxième ouverture : rien de plus à migrer
    with db.transaction() as c:
        c.execute(
            "INSERT INTO registre_domaines(registre, domaine, maj, ville) VALUES ('r','y.fr','2026-10-01','Lyon')"
        )
    db.close()
    assert Database(path).conn.execute("SELECT COUNT(*) FROM registre_domaines").fetchone()[0] == 1
