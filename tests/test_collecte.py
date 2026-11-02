"""Collecte et ingestion automatique (FINAL.md § 7) : faux serveur local, aucune requête réelle."""

import pytest
import yaml

from fakes_pages import PagesServer
from grappes.autonomy import Autonomy, load_subjects
from grappes.clustering.assign import level
from grappes.config import load_config
from grappes.cycle.collect import best_subject, collect, proximity
from grappes.fetch import PageFetcher
from grappes.learned import LearnedTerm, save_seed
from grappes.pipeline import run
from grappes.search.config import load_search_config
from grappes.storage.db import Database

PHRASE = "jardins partagés et potagers collectifs en ville"
SEED = {
    "jardins": 0.4,
    "partages": 0.4,
    "potager": 0.4,
    "compost": 0.4,
    "semis": 0.4,
    "collectif": 0.4,
}


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def srv():
    with PagesServer(robots=(200, "User-agent: *\nDisallow: /interdit/\n")) as s:
        yield s


def monde(tmp_path, srv, seed=True, **auto):
    cfgdir = tmp_path / "config"
    (cfgdir / "recherche").mkdir(parents=True)
    (cfgdir / "recherche" / "api.yaml").write_text(
        f"id: api\ntype: http\nurl: {srv.base}/api\nparams: {{q: '{{sujet}}'}}\nresponse: {{items: results}}\n"
        "fields: {title: title, url: link, description: snippet}\n"
    )
    (cfgdir / "recherche.yaml").write_text("actifs: [api]\ndelai_s: 0\n")
    (cfgdir / "sujets.yaml").write_text(yaml.safe_dump({"sujets": [PHRASE]}, allow_unicode=True))
    (subject,) = load_subjects(cfgdir).subjects
    if seed:
        save_seed(cfgdir, subject.id, {t: LearnedTerm(w, 0, 0) for t, w in SEED.items()})
    cfg = load_config(cfgdir)
    scfg = load_search_config(cfgdir)
    clock = Clock()
    autonomy = Autonomy(**{"seuil_ingestion": 20.0, "interdits_motifs": ("motifinterdit",), **auto})
    fetcher = PageFetcher(
        autonomy, sleep=clock.sleep, monotonic=clock.monotonic, allow_private=True
    )
    db = Database(tmp_path / "c.db")
    return cfg, scfg, autonomy, fetcher, db


def lancer(tmp_path, srv, execute=True, **auto):
    cfg, scfg, autonomy, fetcher, db = monde(tmp_path, srv, **auto)
    rep = collect(db, cfg, scfg, autonomy, fetcher, execute=execute, log=False)
    return cfg, db, rep, fetcher


# --- dry-run -----------------------------------------------------------------------------------


def test_dry_run_ne_fait_aucun_appel_et_n_ecrit_rien(tmp_path, srv):
    _cfg, db, rep, _ = lancer(tmp_path, srv, execute=False)
    assert rep.dry_run and srv.log == [] and db.load_observations() == []
    assert rep.requests == 0 and rep.ingested == []


def test_sujet_sans_amorcage_ignore(tmp_path, srv):
    cfg, _db, rep, _ = lancer(tmp_path, srv, seed=False)
    assert srv.log == [] and rep.skipped_subjects == [cfg.grappes[0].grappe_id]


# --- règles d'ingestion --------------------------------------------------------------------------


def test_ingestion_automatique_des_candidats_proches_et_publics(tmp_path, srv):
    _cfg, db, rep, _ = lancer(tmp_path, srv)
    urls = {i.url.rsplit("/", 1)[-1] for i in rep.ingested}
    assert {"jardin-a", "jardin-b"} <= urls  # candidats : proches du sujet, publics, autorisés
    assert rep.observations_new == len(rep.ingested) == len(db.load_observations())
    assert all(i.score >= 20 for i in rep.ingested)
    assert all(o.source.ecosystem == "web" for o in db.load_observations())


def test_profondeur_explore_les_liens_des_pages_ingerees(tmp_path, srv):
    _, _db, rep, _ = lancer(tmp_path, srv, profondeur=2)
    assert {"jardin-c", "jardin-d"} <= {i.url.rsplit("/", 1)[-1] for i in rep.ingested}
    assert {i.depth for i in rep.ingested if i.url.endswith("jardin-c")} == {1}
    assert {i.depth for i in rep.ingested if i.url.endswith("jardin-d")} <= {
        0,
        2,
    }  # candidat direct ou lien
    _, _db0, rep0, _ = lancer(tmp_path / "p0", srv, profondeur=0)
    assert all(i.depth == 0 for i in rep0.ingested)
    assert "jardin-c" not in {i.url.rsplit("/", 1)[-1] for i in rep0.ingested}


def test_refus_avec_motif(tmp_path, srv):
    _, _, rep, _ = lancer(tmp_path, srv)
    motifs = " | ".join(rep.refused)
    assert "proximité insuffisante" in motifs  # « loin »
    assert "réseau social" in motifs  # x.com : citation seulement
    assert "motif interdit" in motifs
    assert (
        "robots.txt" in motifs or "visite interdite" in motifs or srv.hits("/interdit/secret") == 0
    )
    assert srv.hits("/hors-sujet") == 0 and srv.hits("/interdit/secret") == 0  # jamais visités
    assert "connexion requise" in motifs  # /login
    assert any("403" in k for k in rep.refused)


def test_reseaux_sociaux_jamais_lus_ni_ingeres(tmp_path, srv):
    _, db, _rep, _ = lancer(tmp_path, srv)
    assert not [o for o in db.load_observations() if o.source.ecosystem != "web"]
    assert all("x.com" not in r["path"] for r in srv.log)


def test_seuil_de_proximite_configurable(tmp_path, srv):
    _, db, rep, _ = lancer(tmp_path, srv, seuil_ingestion=99.9)
    assert rep.ingested == [] and db.load_observations() == []


def test_interdits_domaines_rien_n_est_visite(tmp_path, srv):
    _, _db, rep, _ = lancer(tmp_path, srv, interdits_domaines=("127.0.0.1", "localhost"))
    assert rep.ingested == [] and srv.hits("/jardin-a") == 0
    assert any("domaine interdit" in k for k in rep.refused)


# --- plafonds ----------------------------------------------------------------------------------


def test_plafond_de_pages_par_cycle(tmp_path, srv):
    _, _, rep, _ = lancer(tmp_path, srv, pages_par_cycle=1)
    assert rep.pages_attempted == 1 and len(rep.ingested) == 1
    assert any("plafond de pages" in k for k in rep.refused)


def test_plafond_d_observations_par_cycle(tmp_path, srv):
    _, db, rep, _ = lancer(tmp_path, srv, elements_par_cycle=1)
    assert rep.observations_new == 1 and len(db.load_observations()) == 1
    assert any("plafond d'observations" in k for k in rep.refused)


def test_plafond_de_requetes_de_recherche(tmp_path, srv):
    _, _, rep, _ = lancer(tmp_path, srv, requetes_par_cycle=2)
    assert rep.requests == 2 and srv.hits("/api") == 2  # phrase + un terme, puis quota atteint


def test_plafond_de_nouvelles_sources(tmp_path, srv):
    _, _db, rep, _ = lancer(tmp_path / "libre", srv)
    assert {i.domain for i in rep.ingested} == {
        "127.0.0.1",
        "localhost",
    }  # deux sources, sans plafond
    _, _db1, rep1, _ = lancer(tmp_path / "plafonne", srv, nouvelles_sources_par_cycle=1)
    assert len(rep1.new_sources) == 1 and len({i.domain for i in rep1.ingested}) == 1
    assert any("plafond de nouvelles sources" in k for k in rep1.refused)


def test_plafond_de_pages_par_domaine(tmp_path, srv, monkeypatch):
    from grappes.cycle import collect as mod

    monkeypatch.setattr(mod, "MAX_PAGES_PER_DOMAIN", 1)
    _, _, rep, _ = lancer(tmp_path, srv)
    par_domaine = {}
    for i in rep.ingested:
        par_domaine[i.domain] = par_domaine.get(i.domain, 0) + 1
    assert max(par_domaine.values()) == 1
    assert any("plafond de pages par domaine" in k for k in rep.refused)


# --- arrêt, idempotence, rôle du recoupement --------------------------------------------------


def test_fichier_stop_arrete_la_collecte(tmp_path, srv):
    cfg, scfg, autonomy, fetcher, db = monde(tmp_path, srv)
    rep = collect(
        db, cfg, scfg, autonomy, fetcher, execute=True, should_stop=lambda: True, log=False
    )
    assert srv.log == [] and rep.ingested == []


def test_idempotent_une_page_deja_ingeree_n_est_pas_reprise(tmp_path, srv):
    cfg, scfg, autonomy, fetcher, db = monde(tmp_path, srv)
    r1 = collect(db, cfg, scfg, autonomy, fetcher, execute=True, log=False)
    n = len(db.load_observations())
    r2 = collect(
        db,
        cfg,
        scfg,
        autonomy,
        PageFetcher(autonomy, allow_private=True, sleep=lambda s: None),
        execute=True,
        log=False,
    )
    assert r1.ingested and r2.ingested == [] and len(db.load_observations()) == n
    assert r2.refused["page déjà ingérée"] >= 1


def test_une_source_non_recoupee_reste_probable_au_mieux(tmp_path, srv):
    cfg, db, _rep, _ = lancer(tmp_path, srv)
    _, res = run(db, cfg)
    assert res.entities
    for m in res.memberships:
        assert m["via"] != "recoupement"  # rien n'est recoupé : une seule source, aucun citant
        assert level(m["confidence"], cfg, m["via"] == "recoupement") in ("probable", "faible")


def test_proximite_et_choix_du_sujet(tmp_path, srv):
    cfg, *_ = monde(tmp_path, srv)
    g = cfg.grappes[0]
    assert proximity("jardins partagés, potager collectif, compost et semis", g) > 50
    assert proximity("garage voiture lundi", g) == 0 and proximity("   ", g) == 0
    assert best_subject("compost semis", [g])[0] is g and best_subject("rien", [g])[0] is None


# --- leçons des premières données réelles ----------------------------------------------------------------


def test_liens_de_pied_de_page_non_suivis(tmp_path, srv):
    """Un lien dont ni le texte ni l'adresse ne touchent au sujet (« mentions légales ») n'est pas exploré."""
    _, _db, rep, _ = lancer(tmp_path, srv, profondeur=2)
    assert srv.hits("/hors-sujet") == 0  # la page jardin-a le lie, mais sans rapport avec le sujet
    assert {"jardin-c", "jardin-d"} <= {
        i.url.rsplit("/", 1)[-1] for i in rep.ingested
    }  # les liens pertinents, si


def test_requetes_phrase_puis_termes_combines_par_deux(tmp_path, srv):
    from grappes.cycle.collect import search_queries

    cfg, *_ = monde(tmp_path, srv)
    q = search_queries(cfg.grappes[0])
    assert q[0] == cfg.grappes[0].subject  # la phrase d'abord
    assert all(" " in x for x in q[1:]) and len(q) <= 3  # jamais un mot seul : trop de bruit


# --- plafond de nouvelles sources partagé entre écosystèmes (constat du cycle 5) ------------------------------

PLUGIN_MIXTE = """
from grappes.search import Candidate, SearchProvider, register_provider


@register_provider
class Mixte(SearchProvider):
    type = "mixte_test"

    def search(self, keyword, limit):
        base = self.options["base"]
        texte = "Jardins partagés et potager collectif : compost, semis et récolte en ville."
        for i in range(8):
            doc = {"ecosysteme": "wikipedia", "source": f"fr/article_{i}", "nom": f"Article {i}", "liens": [], "exclu": ""}
            yield Candidate("wikipedia", doc["source"], f"https://fr.wikipedia.org/wiki/Article_{i}", keyword, self.id, "doc", text=texte, doc=doc)
        if self.options.get("avec_pages", True):
            yield Candidate("web", "127.0.0.1", f"{base}/jardin-a", keyword, self.id, "page", text=texte)
            yield Candidate("web", "localhost", f"{base.replace('127.0.0.1', 'localhost')}/jardin-b", keyword, self.id, "page", text=texte)
"""


def monde_mixte(tmp_path, srv, avec_pages, **auto):
    cfg, _scfg, autonomy, fetcher, db = monde(tmp_path, srv, **auto)
    cfgdir = tmp_path / "config"
    (cfgdir / "recherche" / "api.yaml").unlink()
    (cfgdir / "recherche" / "mixte.yaml").write_text(
        f"id: mixte\ntype: mixte_test\nbase: {srv.base}\navec_pages: {str(avec_pages).lower()}\n"
    )
    (cfgdir / "recherche.yaml").write_text("actifs: [mixte]\ndelai_s: 0\n")
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "mixte.py").write_text(PLUGIN_MIXTE)
    return cfg, load_search_config(cfgdir), autonomy, fetcher, db


def test_les_pages_web_gardent_leur_place_face_aux_documents(tmp_path, srv):
    cfg, scfg, autonomy, fetcher, db = monde_mixte(
        tmp_path, srv, True, nouvelles_sources_par_cycle=4
    )
    rep = collect(db, cfg, scfg, autonomy, fetcher, execute=True, log=False)
    par_eco = {}
    for i in rep.ingested:
        if i.new_source:
            par_eco[i.ref.split(":")[0]] = par_eco.get(i.ref.split(":")[0], 0) + 1
    assert par_eco == {
        "wikipedia": 2,
        "web": 2,
    }  # équitable : sans cela, Wikipédia prenait les 4 places
    assert rep.refused["plafond de nouvelles sources atteint"] == 6


def test_places_inutilisees_redistribuees_aux_documents(tmp_path, srv):
    cfg, scfg, autonomy, fetcher, db = monde_mixte(
        tmp_path, srv, False, nouvelles_sources_par_cycle=4
    )
    rep = collect(db, cfg, scfg, autonomy, fetcher, execute=True, log=False)
    assert len(rep.new_sources) == 4 and {i.ref.split(":")[0] for i in rep.ingested} == {
        "wikipedia"
    }  # pas de page : tout aux documents


# --- un mot répété ne fait pas un sujet (constat du cycle 6) ----------------------------------------------------


def test_radicaux_pluriel_singulier_confondus():
    from grappes.cycle.collect import stem

    assert stem("blocages") == stem("blocage") == "blocage" and stem("étudiants") == stem(
        "etudiant"
    )
    assert stem("france") == "france" and stem("bus") == "bus" and stem("#cuisine") == "cuisine"


def test_ingestion_exige_deux_radicaux_distincts(tmp_path, srv):
    from grappes.cycle.collect import best_subject, distinct_terms

    cfg, *_ = monde(tmp_path, srv)
    g = cfg.grappes[0]
    seul = (
        "compost compost compost compost compost compost"  # un seul mot, répété : jamais un sujet
    )
    assert proximity(seul, g) > 20 and distinct_terms(seul, g) == 1
    assert (
        best_subject(seul, [g], min_terms=2)[0] is None
        and best_subject(seul, [g], min_terms=1)[0] is g
    )
    deux = "le compost du potager"
    assert distinct_terms(deux, g) == 2 and best_subject(deux, [g], min_terms=2)[0] is g
    assert distinct_terms("potager potagers", g) == 1  # singulier et pluriel : le même radical


def test_requetes_sans_variantes_du_meme_mot(tmp_path, srv):
    from grappes.cycle.collect import search_queries, stem
    from grappes.models import Grappe, Lexicon

    g = Grappe(
        "g",
        "g",
        Lexicon(
            terms={
                "acteur": 0.5,
                "acteurs": 0.5,
                "blocage": 0.5,
                "blocages": 0.5,
                "etudiant": 0.4,
                "etudiants": 0.4,
            }
        ),
        subject="la phrase du sujet",
    )
    q = search_queries(g)
    assert q[0] == "la phrase du sujet"
    for requete in q[1:]:
        stems = [stem(w) for w in requete.split()]
        assert len(stems) == len(set(stems)) == 2  # jamais « acteur acteurs »
    assert len(q) == 3 and "acteur" in q[1] and "etudiant" in q[2]


def test_deux_radicaux_dans_l_ingestion_reelle(tmp_path, srv):
    """Sur le faux serveur, toute page ingérée contient au moins deux radicaux du lexique."""
    cfg, db, _rep, _ = lancer(tmp_path, srv)
    from grappes.cycle.collect import distinct_terms

    g = cfg.grappes[0]
    assert db.load_observations() and all(
        distinct_terms(o.text, g) >= 2 for o in db.load_observations()
    )
