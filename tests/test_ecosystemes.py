"""Q22 : RSS et Wikipédia comme fournisseurs, chacun sous son propre écosystème (« rss », « wikipedia »),
distincts de « web » (Tavily), pour restaurer un vrai recoupement entre écosystèmes. Faux serveurs locaux."""

from pathlib import Path

import pytest
import yaml

from conftest import CONFIG_CLAIR
from fakes_recherche import FakeServer
from grappes.autonomy import Autonomy
from grappes.clustering.assign import assign
from grappes.config import load_config
from grappes.cycle.collect import collect
from grappes.fetch import PageFetcher
from grappes.search.build import build_candidate
from grappes.search.config import load_search_config
from grappes.search.engine import run_search
from grappes.storage.db import Database
from test_phase4 import STRONG, make_cfg, obs

ROOT = Path(__file__).resolve().parents[1]
PHRASE = "Les acteurs des blocages et mobilisations étudiantes en France en ce moment"


def spec(nom: str) -> dict:
    return yaml.safe_load((ROOT / "config" / "recherche" / f"{nom}.yaml").read_text("utf-8"))


def projet(tmp_path, base, noms=("wikipedia", "rss_letudiant"), robots=None):
    """Charge les VRAIS fichiers de config/recherche/ en ne remplaçant que l'URL par le faux serveur."""
    cfg = tmp_path / "config"
    (cfg / "recherche").mkdir(parents=True)
    for nom in noms:
        s = spec(nom)
        s["url"] = f"{base}/wikipedia" if nom == "wikipedia" else f"{base}/flux-etudiant"
        (cfg / "recherche" / f"{nom}.yaml").write_text(
            yaml.safe_dump(s, allow_unicode=True), "utf-8"
        )
    (cfg / "recherche.yaml").write_text(
        f"actifs: [{', '.join(noms)}]\ndelai_s: 0\nlimite_par_mot_cle: 10\n", "utf-8"
    )
    return load_search_config(cfg)


@pytest.fixture
def srv():
    with FakeServer() as s:
        yield s


# --- configuration livrée ------------------------------------------------------------------------------


def test_fournisseurs_livres_et_ecosystemes_distincts_de_web():
    scfg = load_search_config(ROOT / "config")
    assert {"tavily", "wikipedia", "rss_letudiant", "rss_liberation"} <= set(scfg.actifs)
    eco = {p: s.options.get("observation", {}).get("ecosysteme") for p, s in scfg.providers.items()}
    assert (
        eco["wikipedia"] == "wikipedia" and eco["rss_letudiant"] == eco["rss_liberation"] == "rss"
    )
    assert eco["tavily"] is None  # Tavily : pages web récupérées, écosystème « web »
    for nom in ("rss_letudiant", "rss_liberation"):
        o = scfg.providers[nom].options
        assert (
            o["respecter_robots"] is True and o["sans_mots_cles"] is True
        )  # flux lu une fois, robots respecté
    assert not (ROOT / "config" / "recherche" / "wikidata.yaml").exists()  # choix documenté (D46)


def test_types_d_entites_des_nouveaux_ecosystemes():
    sc = load_config(CONFIG_CLAIR).scoring
    assert sc.entity_types["rss"] == "site" and sc.entity_types["wikipedia"] == "group"


# --- le résultat EST le document ---------------------------------------------------------------------------


def test_candidat_wikipedia_document(srv, tmp_path):
    rep = run_search(projet(tmp_path, srv.base, ("wikipedia",)), ["blocages"], execute=True)
    par_titre = {c.doc["nom"]: c for c in rep.raw}
    union = par_titre["Union nationale des étudiants"]
    assert (union.ecosystem, union.doc["ecosysteme"]) == ("wikipedia", "wikipedia")
    assert union.doc["source"] == "fr/union_nationale_des_étudiants" and not union.doc["exclu"]
    assert union.doc["liens"] == [
        "https://unef-demo.test",
        "https://exemple-presse.test/article",
        "https://x.com/unef_demo",
    ]
    assert union.url.startswith("https://fr.wikipedia.org/wiki/") and "blocages" in union.text


def test_personnes_physiques_et_cas_douteux_non_ingerables(srv, tmp_path):
    rep = run_search(projet(tmp_path, srv.base, ("wikipedia",)), ["blocages"], execute=True)
    exclu = {c.doc["nom"]: c.doc["exclu"] for c in rep.raw}
    assert "catégorie biographique" in exclu["Alice Démo"]  # « Naissance en 1999 »
    assert (
        "texte biographique" in exclu["Bob Démo"]
    )  # « (né le 3 mars 1980) », sans catégorie de naissance
    assert "en cas de doute" in exclu["Article sans catégorie"]
    assert (
        exclu["Union nationale des étudiants"] == ""
        and exclu["Blocages universitaires de 2026"] == ""
    )


def test_flux_rss_document_sous_l_ecosysteme_rss(srv, tmp_path):
    rep = run_search(projet(tmp_path, srv.base, ("rss_letudiant",)), ["a", "b", "c"], execute=True)
    assert len(srv.log) == 2 and [r["path"] for r in srv.log] == [
        "/robots.txt",
        "/flux-etudiant",
    ]  # lu UNE fois malgré 3 mots-clés
    assert len(rep.raw) == 3
    c = rep.raw[0]
    assert (c.ecosystem, c.ident) == ("rss", "letudiant.fr") and c.doc["nom"] == "L'Étudiant"
    assert (
        c.url == "https://flux-etudiant.test/a1" and c.date == "2026-10-09" and c.doc["liens"] == []
    )


def test_flux_robots_interdit_aucun_acces_au_flux(tmp_path):
    with FakeServer() as srv:
        srv.httpd.robots = (200, "User-agent: *\nDisallow: /flux-etudiant\n")
        rep = run_search(projet(tmp_path, srv.base, ("rss_letudiant",)), ["a"], execute=True)
        assert [r["path"] for r in srv.log] == ["/robots.txt"] and rep.raw == []
        assert "robots.txt interdit" in rep.status["rss_letudiant"]
    with FakeServer() as srv:
        srv.httpd.robots = (403, "")
        rep = run_search(projet(tmp_path / "b", srv.base, ("rss_letudiant",)), ["a"], execute=True)
        assert [r["path"] for r in srv.log] == ["/robots.txt"] and "robots.txt" in rep.status[
            "rss_letudiant"
        ]
    with FakeServer() as srv:
        srv.httpd.robots = (200, "User-agent: *\nAllow: /flux-etudiant\n")
        rep = run_search(projet(tmp_path / "c", srv.base, ("rss_letudiant",)), ["a"], execute=True)
        assert len(rep.raw) == 3


def test_ecosysteme_reserve_ou_invalide_ignore():
    item = {"title": "t", "link": "https://exemple.test/a"}
    base = {"fields": {"title": "title", "url": "link"}}
    for eco in ("web", "x", "Mauvais Nom", ""):
        c = build_candidate(
            "p",
            "k",
            item,
            base["fields"],
            {"observation": {"ecosysteme": eco, "source": {"default": "s"}}},
        )
        assert (
            c.doc is None and c.ecosystem == "web"
        )  # retombe sur le comportement ordinaire (page à récupérer)


# --- ingestion directe -------------------------------------------------------------------------------------------


class SansPage:
    """Transport qui échoue si on tente de récupérer une page : les documents ne passent pas par là."""

    def __call__(self, *a, **k):
        raise AssertionError("aucune page ne doit être récupérée pour un résultat-document")


def lancer(tmp_path, srv, **auto):
    scfg = projet(tmp_path, srv.base)
    # lexique du sujet : mots d'un sujet « étudiant » (écrit en dur pour le test, comme le ferait l'amorçage)
    cfgdir = tmp_path / "config"
    (cfgdir / "sujets.yaml").write_text(
        yaml.safe_dump({"sujets": [{"nom": "mob", "phrase": PHRASE}]}, allow_unicode=True)
    )
    from grappes.learned import LearnedTerm, save_seed

    terms = {
        "etudiant": 0.5,
        "etudiante": 0.5,
        "etudiants": 0.5,
        "blocages": 0.5,
        "mobilisation": 0.5,
        "mobilisations": 0.5,
        "universites": 0.5,
        "syndicats": 0.5,
    }
    save_seed(cfgdir, "mob", {t: LearnedTerm(w, 0, 0) for t, w in terms.items()})
    cfg = load_config(cfgdir)
    autonomy = Autonomy(**{"seuil_ingestion": 20.0, **auto})
    fetcher = PageFetcher(autonomy, transport=SansPage(), sleep=lambda s: None, allow_private=True)
    db = Database(tmp_path / "e.db")
    rep = collect(db, cfg, scfg, autonomy, fetcher, execute=True, log=False)
    return cfg, db, rep


def test_ingestion_directe_sous_les_ecosystemes_propres(tmp_path, srv):
    _cfg, db, _rep = lancer(tmp_path, srv)
    sources = {o.source.key: o for o in db.load_observations()}
    assert "wikipedia:fr/union_nationale_des_étudiants" in sources and "rss:letudiant.fr" in sources
    assert {k.split(":")[0] for k in sources} == {"wikipedia", "rss"}  # jamais « web » ici
    assert db.load_display_names()["rss:letudiant.fr"] == "L'Étudiant"
    assert (
        db.load_display_names()["wikipedia:fr/union_nationale_des_étudiants"]
        == "Union nationale des étudiants"
    )
    assert set(db.origins().values()) == {"recherche"}
    wiki = sources["wikipedia:fr/union_nationale_des_étudiants"]
    assert "https://unef-demo.test" in wiki.links  # les liens externes deviennent des citations


def test_exclusions_et_proximite_comptees_en_clair(tmp_path, srv):
    _, db, rep = lancer(tmp_path, srv)
    refus = " | ".join(rep.refused)
    assert (
        "catégorie biographique" in refus
        and "texte biographique" in refus
        and "en cas de doute" in refus
    )
    assert "proximité insuffisante" in refus  # la soupe, le gratin
    noms = {o.source.key for o in db.load_observations()}
    assert not any("alice" in k or "bob" in k for k in noms)  # aucune personne ingérée
    assert not any("soupe" in k or "sans_cat" in k for k in noms)


def test_flux_ingere_plusieurs_elements_une_seule_source(tmp_path, srv):
    _, db, rep = lancer(tmp_path, srv)
    rss = [o for o in db.load_observations() if o.source.ecosystem == "rss"]
    assert len(rss) == 2 and {o.source.key for o in rss} == {
        "rss:letudiant.fr"
    }  # a1 et a3 ; le gratin est écarté
    assert sum(1 for i in rep.ingested if i.ref == "rss:letudiant.fr" and i.new_source) == 1


def test_documents_idempotents_et_plafonnes(tmp_path, srv):
    cfg, db, _rep = lancer(tmp_path, srv)
    n = len(db.load_observations())
    scfg = load_search_config(tmp_path / "config")
    autonomy = Autonomy(seuil_ingestion=20.0)
    fetcher = PageFetcher(autonomy, transport=SansPage(), sleep=lambda s: None, allow_private=True)
    rep2 = collect(db, cfg, scfg, autonomy, fetcher, execute=True, log=False)
    assert len(db.load_observations()) == n and rep2.refused["document déjà ingéré"] >= 3
    _, _, plafond = lancer(tmp_path / "p", srv, elements_par_cycle=1)
    assert plafond.observations_new == 1
    _, _db_s, sources = lancer(tmp_path / "s", srv, nouvelles_sources_par_cycle=1)
    assert len(sources.new_sources) == 1


def test_interdits_valables_pour_les_documents(tmp_path, srv):
    _, db, rep = lancer(
        tmp_path, srv, interdits_domaines=("flux-etudiant.test",), interdits_motifs=("wikipedia",)
    )
    assert db.load_observations() == []
    assert any("domaine interdit" in k or "motif interdit" in k for k in rep.refused)


def test_aucune_page_recuperee_pour_les_documents(tmp_path, srv):
    lancer(
        tmp_path, srv
    )  # SansPage lève si une page est demandée ; le simple fait de finir le prouve
    assert all(r["path"] in ("/robots.txt", "/flux-etudiant", "/wikipedia") for r in srv.log)


# --- le recoupement entre écosystèmes -----------------------------------------------------------------------


def test_recoupement_entre_ecosystemes_wikipedia_rss_web(tmp_path):
    """Trois citants de trois écosystèmes pour un même site : recoupement validé, E > 0, classé via recoupement."""
    (tmp_path / "c").mkdir()
    cfg = make_cfg(tmp_path / "c", seuil_min=40)
    cible = "https://www.unef-demo.test/"
    o = [obs("web:cible-texte.test", "rien")]  # (la cible a aussi un texte propre, neutre)
    o += [obs("web:unef-demo.test", "rien de précis")]
    for src in (
        "wikipedia:fr/article_un",
        "wikipedia:fr/article_deux",
        "rss:letudiant.fr",
        "web:presse.test",
    ):
        o += [obs(src, STRONG), obs(src, "neutre", links=[cible])]
    res = assign(o, cfg)
    rec = next(
        r for r in res.validations if r.cited == "web:unef-demo.test" and r.grappe_id == "G-A"
    )
    assert (
        rec.validated
        and set(rec.ecosystems) == {"wikipedia", "rss", "web"}
        and len(rec.citers) == 4
    )
    eid = next(e.entity_id for e in res.entities if e.primary_ref == "web:unef-demo.test")
    m = next(m for m in res.memberships if m["entity_id"] == eid and m["grappe_id"] == "G-A")
    assert m["via"] == "recoupement" and m["status"] == "classé" and m["ecosystem"] > 0


def test_un_seul_ecosysteme_ne_suffit_pas_meme_avec_beaucoup_de_citants(tmp_path):
    (tmp_path / "c").mkdir()
    cfg = make_cfg(tmp_path / "c", seuil_min=40)
    o = [obs("web:cible.test", "rien")]
    for i in range(6):
        o += [
            obs(f"web:s{i}.test", STRONG),
            obs(f"web:s{i}.test", "neutre", links=["https://cible.test/"]),
        ]
    res = assign(o, cfg)
    rec = next(r for r in res.validations if r.cited == "web:cible.test" and r.grappe_id == "G-A")
    assert not rec.validated and "écosystèmes 1 < 2" in " ".join(rec.reasons)
