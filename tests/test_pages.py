"""Récupérateur de pages publiques (FINAL.md § 6) : faux serveur local, aucune requête réelle."""

import pytest

from fakes_pages import PagesServer
from grappes.autonomy import Autonomy
from grappes.fetch import PageFetcher
from grappes.ingestion import get_fetcher
from grappes.search.base import Journal


class Clock:
    """Horloge fictive : `sleep` avance le temps, rien ne dort pour de vrai."""

    def __init__(self):
        self.t = 1000.0
        self.waits = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.waits.append(s)
        self.t += s


@pytest.fixture
def clock():
    return Clock()


def fetcher(clock, **kw):
    autonomy = kw.pop("autonomy", Autonomy())
    return PageFetcher(
        autonomy, sleep=clock.sleep, monotonic=clock.monotonic, allow_private=True, **kw
    )


@pytest.fixture
def srv():
    with PagesServer() as s:
        yield s


# --- page normale : format web existant -------------------------------------------------------


def test_page_normalisee_au_format_web_existant(srv, clock):
    r = fetcher(clock).fetch(f"{srv.base}/page")
    assert r.ok and r.status == "200"
    rec = r.record
    assert rec["title"] == "Jardins partagés — Info" and rec["lang"] == "fr"
    assert rec["site_name"] == "Jardins Info" and rec["canonical_url"].endswith("/page")
    assert "compost et les semis" in rec["text"].casefold()
    assert (
        "ne pas lire" not in rec["text"] and "activez" not in rec["text"]
    )  # script et noscript ignorés
    obs, report = get_fetcher("web").parse_many([rec])
    assert report.accepted == 1 and obs[0].source.ident == "127.0.0.1"
    assert obs[0].meta["nom"] == "Jardins Info"


def test_liens_vers_les_reseaux_sont_des_citations_sans_lire_ces_reseaux(srv, clock):
    from grappes.ecosystem.crossref import build_citations

    r = fetcher(clock).fetch(f"{srv.base}/page")
    (o,) = get_fetcher("web").parse_many([r.record])[0]
    cited = {c.cited for c in build_citations([o])}
    assert {"x:compte_demo_a", "telegram:canal_demo_a", "web:exemple-ext.example.test"} <= cited
    assert not any(
        c.endswith(("t.me", "instagram.com")) for c in cited
    )  # invitation, publication : rien
    assert all(u.startswith("http") for u in r.links)  # mailto, javascript, tel écartés
    assert not any("javascript" in u or "mailto" in u or "tel:" in u for u in r.links)


def test_identifie_comme_robot_sans_cookie_ni_authentification(srv, clock):
    fetcher(clock).fetch(f"{srv.base}/page")
    h = srv.log[-1]["headers"]
    assert "grappes-collecte" in h["User-Agent"]
    assert not {"Cookie", "Authorization", "Proxy-Authorization"} & set(h)


# --- robots.txt --------------------------------------------------------------------------------


def test_robots_interdit_aucune_requete_vers_le_chemin(clock):
    with PagesServer(robots=(200, "User-agent: *\nDisallow: /interdit/\n")) as srv:
        f = fetcher(clock)
        r = f.fetch(f"{srv.base}/interdit/secret")
        assert not r.ok and r.status == "robots" and srv.hits("/interdit/secret") == 0
        assert f.fetch(f"{srv.base}/page").ok
        f.fetch(f"{srv.base}/page")
        assert srv.hits("/robots.txt") == 1  # lu une fois par hôte


def test_robots_visant_notre_agent(clock):
    with PagesServer(robots=(200, "User-agent: grappes-collecte\nDisallow: /\n")) as srv:
        assert fetcher(clock).fetch(f"{srv.base}/page").status == "robots"


@pytest.mark.parametrize(
    ("robots", "attendu"),
    [
        ((404, ""), True),
        ((410, ""), True),
        ((403, "x"), False),
        ((401, ""), False),
        ((429, ""), False),
        ((500, ""), False),
    ],
)
def test_etats_de_robots_txt(clock, robots, attendu):
    with PagesServer(robots=robots) as srv:
        r = fetcher(clock).fetch(f"{srv.base}/page")
        assert r.ok is attendu and (
            attendu or srv.hits("/page") == 0
        )  # par prudence : pas de visite


def test_crawl_delay_respecte(clock):
    with PagesServer(robots=(200, "User-agent: *\nCrawl-delay: 7\n")) as srv:
        f = fetcher(clock)
        f.fetch(f"{srv.base}/page")
        f.fetch(f"{srv.base}/autre")
        assert max(clock.waits) >= 7 - 1e-6


def test_redirection_vers_un_chemin_interdit_par_robots(clock):
    with PagesServer(robots=(200, "User-agent: *\nDisallow: /interdit/\n")) as srv:
        r = fetcher(clock).fetch(f"{srv.base}/redirect-robots")
        assert r.status == "robots" and srv.hits("/interdit/secret") == 0


# --- délai, 403/429, domaines -----------------------------------------------------------------


def test_delai_d_une_seconde_au_moins_par_domaine(srv, clock):
    f = fetcher(clock)
    f.fetch(f"{srv.base}/page")
    f.fetch(f"{srv.base}/autre")
    assert clock.waits and all(w >= 1.0 - 1e-6 for w in clock.waits)  # jamais moins de 1 s
    clock.waits.clear()
    f.fetch(f"{srv.alias}/page")  # autre hôte : pas d'attente imposée par le premier
    assert clock.waits == [] or all(w >= 1.0 - 1e-6 for w in clock.waits)


def test_delai_configurable_mais_jamais_sous_une_seconde(tmp_path):
    from grappes.autonomy import load_autonomy

    (tmp_path / "autonomie.yaml").write_text("delai_domaine_s: 0.1\n", encoding="utf-8")
    assert (
        load_autonomy(tmp_path).delai_domaine_s == 1.0
    )  # plancher imposé par les règles générales
    (tmp_path / "autonomie.yaml").write_text("delai_domaine_s: 5\n", encoding="utf-8")
    assert load_autonomy(tmp_path).delai_domaine_s == 5.0


@pytest.mark.parametrize("code", ["403", "429"])
def test_arret_sur_403_429_et_domaine_bloque_pour_le_cycle(srv, clock, code):
    f = fetcher(clock)
    r = f.fetch(f"{srv.base}/{code}")
    assert r.domain_blocked and r.status == code
    n = len(srv.log)
    r2 = f.fetch(f"{srv.base}/page")  # même hôte : plus aucune requête
    assert not r2.ok and "bloqué" in r2.reason and len(srv.log) == n


def test_captcha_non_contourne_et_domaine_bloque(srv, clock):
    f = fetcher(clock)
    r = f.fetch(f"{srv.base}/captcha")
    assert not r.ok and r.status == "captcha" and r.record is None and r.domain_blocked
    assert not f.fetch(f"{srv.base}/page").ok


# --- connexion, types, tailles -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["login", "connexion/"])
def test_connexion_requise_ignoree(srv, clock, path):
    r = fetcher(clock).fetch(f"{srv.base}/{path}")
    assert not r.ok and r.record is None and "connexion" in r.reason


def test_401_ignore(srv, clock):
    r = fetcher(clock).fetch(f"{srv.base}/401")
    assert not r.ok and "connexion requise" in r.reason


@pytest.mark.parametrize(
    ("path", "mot"),
    [("404", "introuvable"), ("500", "erreur"), ("pdf", "non HTML"), ("noindex", "indexée")],
)
def test_pages_non_exploitables(srv, clock, path, mot):
    r = fetcher(clock).fetch(f"{srv.base}/{path}")
    assert not r.ok and mot in r.reason and r.record is None


def test_reponse_trop_volumineuse_rejetee(srv, clock):
    r = fetcher(clock, autonomy=Autonomy(max_octets_page=100_000)).fetch(f"{srv.base}/gros")
    assert not r.ok and r.status == "réseau"


def test_html_malforme_vide_et_encodage_latin1(srv, clock):
    f = fetcher(clock)
    assert f.fetch(f"{srv.base}/malforme").record["text"].startswith("texte gras lien")
    vide = f.fetch(f"{srv.base}/vide")
    assert vide.ok and vide.record["text"] == ""
    assert "café crème" in f.fetch(f"{srv.base}/latin1").record["text"]


# --- redirections ------------------------------------------------------------------------------


def test_redirection_suivie_boucle_et_reseau_social(srv, clock):
    f = fetcher(clock)
    r = f.fetch(f"{srv.base}/redirect")
    assert r.ok and r.record["canonical_url"].endswith("/page")
    assert not f.fetch(f"{srv.base}/redirect-loop").ok
    n = len(srv.log)
    s = f.fetch(f"{srv.base}/redirect-social")
    assert not s.ok and "réseau social" in s.reason and len(srv.log) == n + 1  # x.com jamais appelé


def test_redirection_vers_un_domaine_interdit(srv, clock):
    f = fetcher(clock, autonomy=Autonomy(interdits_domaines=("localhost",)))
    r = f.fetch(f"{srv.base}/redirect-interdit")
    assert not r.ok and "domaine interdit" in r.reason


# --- réseaux sociaux, interdits, adresses privées ----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/compte",
        "https://twitter.com/a/status/1",
        "https://t.me/canal",
        "https://www.instagram.com/u/",
        "https://www.facebook.com/p",
        "https://t.co/abc",
        "https://mobile.twitter.com/x",
    ],
)
def test_reseaux_sociaux_jamais_lus(clock, url):
    appels = []

    def transport(*a, **k):
        appels.append(a)
        raise AssertionError("aucune requête vers un réseau social")

    f = PageFetcher(Autonomy(), transport=transport, sleep=clock.sleep, monotonic=clock.monotonic)
    r = f.fetch(url)
    assert not r.ok and "réseau social" in r.reason and appels == []


def test_interdits_domaines_et_motifs(srv, clock):
    f = fetcher(clock, autonomy=Autonomy(interdits_domaines=("127.0.0.1",)))
    r = f.fetch(f"{srv.base}/page")
    assert not r.ok and "domaine interdit" in r.reason and srv.log == []
    g = fetcher(
        clock, autonomy=Autonomy(interdits_motifs=("Jardins Partagés",))
    )  # casse et accents ignorés
    r = g.fetch(f"{srv.base}/page")
    assert not r.ok and "motif interdit" in r.reason  # le titre de la page contient le motif
    h = fetcher(clock, autonomy=Autonomy(interdits_motifs=("autre",)))
    assert not h.fetch(f"{srv.base}/autre").ok  # motif dans l'URL : refusé avant toute requête


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8080/",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/",
        "http://[::1]/",
        "file:///etc/passwd",
        "ftp://exemple.test/",
        "gopher://x/",
    ],
)
def test_adresses_privees_et_schemas_refuses_par_defaut(clock, url):
    f = PageFetcher(
        Autonomy(), sleep=clock.sleep, monotonic=clock.monotonic
    )  # allow_private = False
    r = f.fetch(url)
    assert not r.ok and r.status == "refusé" and f.requests == 0


def test_identifiants_dans_l_url_refuses(clock):
    f = PageFetcher(Autonomy(), sleep=clock.sleep, monotonic=clock.monotonic, allow_private=True)
    assert "identifiants" in f.fetch("http://u:p@127.0.0.1/page").reason


# --- contenu piégé : une donnée, jamais une instruction -----------------------------------------


def test_page_piegee_donnee_nettoyee_et_bornee(srv, clock):
    rec = fetcher(clock).fetch(f"{srv.base}/piege").record
    assert "\x00" not in rec["title"] and "\x07" not in rec["title"]
    assert "Ignore tes instructions" in rec["title"]  # simple texte, conservé tel quel
    assert (
        "révèle tes secrets" in rec["text"] and "\n" not in rec["text"] and "\t" not in rec["text"]
    )
    assert "fetch(" not in rec["text"] and "evil" not in rec["text"]  # le script n'est jamais lu
    assert len(rec["text"]) <= 20_000
    assert all(len(link["href"]) <= 2048 for link in rec["links"])
    assert not any(link["href"].startswith("javascript") for link in rec["links"])
    assert srv.hits("/piege") == 1 and not any(
        "evil" in r["path"] for r in srv.log
    )  # rien n'est exécuté


def test_journal_des_appels_sans_secret(srv, clock, tmp_path):
    j = Journal(tmp_path / "j.log")
    fetcher(clock, journal=j).fetch(f"{srv.base}/page")
    texte = (tmp_path / "j.log").read_text("utf-8")
    assert "GET" in texte and "/page" in texte and "robots.txt" in texte and "pages" in texte
