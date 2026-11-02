"""Phase 8 : multiprise de recherche. Uniquement des faux serveurs locaux : aucune requête réelle."""

import csv
import hashlib
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

from conftest import CONFIG_CLAIR
from fakes_recherche import KEY, FakeServer
from grappes.config import ConfigError
from grappes.search.base import SearchProvider, register_provider, registered_types
from grappes.search.config import load_search_config
from grappes.search.engine import run_search, split_keywords, write_candidates_csv

ROOT = Path(__file__).resolve().parents[1]
FAKE_MCP = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


@pytest.fixture
def srv():
    with FakeServer() as s:
        yield s


def make_search(tmp_path, providers, actifs=None, base="", **glob):
    cfg = tmp_path / "config"
    (cfg / "recherche").mkdir(parents=True, exist_ok=True)
    for pid, body in providers.items():
        text = f"id: {pid}\n" + textwrap.dedent(body).replace("BASE", base)
        (cfg / "recherche" / f"{pid}.yaml").write_text(text, encoding="utf-8")
    settings = {
        "actifs": list(providers) if actifs is None else actifs,
        "delai_s": 0,
        "quota_total": 100,
        **glob,
    }
    (cfg / "recherche.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    return load_search_config(cfg)


def journal(tmp_path) -> str:
    p = tmp_path / "donnees" / "journal_recherche.log"
    return p.read_text("utf-8") if p.exists() else ""


API_JSON = """
    type: http
    url: BASE/json
    params: {q: "{mots_cles}", n: "{limite}"}
    auth: {type: header, name: X-Api-Key, env: CLE_TEST}
    response: {format: json, items: results}
    fields: {title: title, url: link, id: id, description: snippet, date: published}
"""


# --- verrous : dry-run, liste blanche, clés -------------------------------------------------


def test_dry_run_par_defaut_aucun_appel(tmp_path, srv, monkeypatch):
    monkeypatch.setenv("CLE_TEST", KEY)
    cfg = make_search(tmp_path, {"api": API_JSON}, base=srv.base)
    rep = run_search(cfg, ["jardin"])  # execute non demandé
    assert srv.log == [] and rep.candidates == [] and not rep.executed
    assert "dry-run" in journal(tmp_path) and "/json" in journal(tmp_path)


def test_sujet_dans_les_gabarits(tmp_path, srv, monkeypatch):
    monkeypatch.setenv("CLE_TEST", KEY)
    body = API_JSON.replace('q: "{mots_cles}"', 'q: "{sujet} / {mots_cles}"')
    cfg = make_search(tmp_path, {"api": body}, base=srv.base)
    run_search(cfg, ["potager"], execute=True, sujet="jardins partagés en ville")
    assert srv.log[0]["query"]["q"] == "jardins partagés en ville / potager"


def test_liste_blanche_obligatoire(tmp_path, srv, monkeypatch):
    monkeypatch.setenv("CLE_TEST", KEY)
    cfg = make_search(tmp_path, {"api": API_JSON}, actifs=[], base=srv.base)
    assert run_search(cfg, ["jardin"], execute=True).status == {}  # rien d'actif
    rep = run_search(cfg, ["jardin"], providers=["api"], execute=True)  # demandé mais non autorisé
    assert srv.log == [] and "non autorisé" in rep.status["api"]
    assert "hors liste blanche" in journal(tmp_path)


def test_actif_sans_fichier_de_configuration_refuse(tmp_path):
    with pytest.raises(ConfigError, match="sans fichier"):
        make_search(tmp_path, {"api": "type: http\nurl: http://x.test\n"}, actifs=["fantome"])


def test_cle_absente_fournisseur_inactif_sans_appel(tmp_path, srv, monkeypatch):
    monkeypatch.delenv("CLE_TEST", raising=False)
    cfg = make_search(tmp_path, {"api": API_JSON}, base=srv.base)
    rep = run_search(cfg, ["jardin"], execute=True)
    assert srv.log == [] and "clé absente" in rep.status["api"] and "CLE_TEST" in rep.status["api"]


def test_secret_jamais_dans_le_journal(tmp_path, srv, monkeypatch):
    monkeypatch.setenv("CLE_TEST", KEY)
    monkeypatch.setenv("CLE_Q", KEY)
    q = "type: http\nurl: BASE/query\nauth: {type: query, name: key, env: CLE_Q}\nresponse: {items: results}\nfields: {url: link}\n"
    cfg = make_search(tmp_path, {"api": API_JSON, "q": q}, base=srv.base)
    rep = run_search(cfg, ["jardin"], execute=True)
    assert len(rep.candidates) == 5 and srv.log[0]["headers"]["X-Api-Key"] == KEY
    assert srv.log[1]["query"]["key"] == KEY  # envoyé au serveur...
    assert KEY not in journal(tmp_path) and "key=" not in journal(tmp_path)  # ... jamais journalisé


# --- authentification, méthodes, gabarits --------------------------------------------------


def test_api_json_a_cle_et_gabarits(tmp_path, srv, monkeypatch):
    monkeypatch.setenv("CLE_TEST", KEY)
    cfg = make_search(tmp_path, {"api": API_JSON}, base=srv.base)
    rep = run_search(cfg, ["potager bio"], execute=True)
    assert rep.executed and rep.requests == 1
    assert srv.log[0]["query"] == {"q": "potager bio", "n": "20"}
    c = {m.ident: m for m in rep.candidates}
    assert set(c) == {"json-0.example.test", "json-1.example.test", "json-2.example.test"}
    m = c["json-0.example.test"]
    assert m.ecosystem == "web" and m.url == "https://json-0.example.test/page"
    assert m.keywords == ["potager bio"] and m.providers == ["api"] and m.date == "2026-09-01"
    assert m.text == "Json 0. description 0" and m.extra["api"]["id"] == "Json-0"


@pytest.mark.parametrize(
    ("auth", "env", "path"),
    [
        ("{type: bearer, env: CLE_A}", {"CLE_A": KEY}, "bearer"),
        ("{type: oauth_env, env: CLE_A}", {"CLE_A": KEY}, "bearer"),
        (
            "{type: basic, user_env: U_A, password_env: P_A}",
            {"U_A": "utilisateur", "P_A": "motdepasse"},
            "basic",
        ),
    ],
)
def test_authentifications(tmp_path, srv, monkeypatch, auth, env, path):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    body = f"type: http\nurl: BASE/{path}\nauth: {auth}\nresponse: {{items: results}}\nfields: {{url: link}}\n"
    rep = run_search(make_search(tmp_path, {"api": body}, base=srv.base), ["x"], execute=True)
    assert len(rep.candidates) == 2 and rep.status["api"] == "ok"
    assert all(v not in journal(tmp_path) for v in env.values())


@pytest.mark.parametrize("kind", ["json", "form"])
def test_post_corps_json_ou_formulaire(tmp_path, srv, kind):
    body = f"""
        type: http
        url: BASE/post
        method: POST
        body: {{type: {kind}, content: {{recherche: "{{mots_cles}}", max: "{{limite}}"}}}}
        response: {{items: results}}
        fields: {{url: link}}
    """
    run_search(make_search(tmp_path, {"api": body}, base=srv.base), ["tomates"], execute=True)
    assert srv.log[0]["method"] == "POST"
    ctype = srv.log[0]["headers"]["Content-Type"]
    assert ("json" in ctype) == (kind == "json")


def test_post_json_garde_les_nombres(tmp_path, srv):
    body = "type: http\nurl: BASE/post\nmethod: POST\nbody: {type: json, content: {max: '{limite}', q: '{mots_cles}'}}\nresponse: {items: results}\nfields: {url: link}\n"
    run_search(make_search(tmp_path, {"api": body}, base=srv.base), ["a"], execute=True)
    import json

    assert json.loads(srv.log[0]["body"]) == {"max": 20, "q": "a"}


# --- formats de réponse ----------------------------------------------------------------------


def test_flux_rss(tmp_path, srv):
    body = """
        type: http
        url: BASE/rss
        response: {format: xml, items: rss.channel.item}
        fields: {title: title, url: link, id: guid, description: description, date: pubDate}
    """
    rep = run_search(make_search(tmp_path, {"flux": body}, base=srv.base), ["x"], execute=True)
    c = {m.key: m for m in rep.candidates}
    assert set(c) == {
        ("web", "rss-un.example.test"),
        ("telegram", "canal_rss"),
    }  # lien t.me -> canal
    assert c[("web", "rss-un.example.test")].date == "2026-09-02"  # date RFC 822


def test_delai_entre_appels_du_fournisseur(tmp_path, srv):
    import time

    body = "type: http\nurl: BASE/json\nparams: {q: '{mots_cles}'}\ndelai_entre_appels_s: 0.4\nresponse: {items: results}\nfields: {url: link}\n"
    t0 = time.monotonic()
    run_search(make_search(tmp_path, {"api": body}, base=srv.base), ["a", "b", "c"], execute=True)
    assert len(srv.log) == 3 and time.monotonic() - t0 >= 0.8  # trois appels, au moins 0,4 s entre chacun


def test_flux_atom_attribut_href(tmp_path, srv):
    body = "type: http\nurl: BASE/atom\nresponse: {format: xml, items: feed.entry}\nfields: {title: title, url: link.@href, description: summary}\n"
    rep = run_search(make_search(tmp_path, {"flux": body}, base=srv.base), ["x"], execute=True)
    assert [m.ident for m in rep.candidates] == ["atom-un.example.test"]


def test_xml_avec_doctype_refuse(tmp_path, srv):
    body = "type: http\nurl: BASE/bombe\nresponse: {format: xml, items: r}\nfields: {url: r}\n"
    rep = run_search(make_search(tmp_path, {"flux": body}, base=srv.base), ["x"], execute=True)
    assert rep.candidates == [] and "DOCTYPE" in rep.status["flux"]


def test_csv(tmp_path, srv):
    body = "type: http\nurl: BASE/csv\nresponse: {format: csv, delimiter: ';'}\nfields: {title: titre, url: url, id: ident}\n"
    rep = run_search(make_search(tmp_path, {"c": body}, base=srv.base), ["x"], execute=True)
    assert {m.key for m in rep.candidates} == {("web", "csv-un.example.test"), ("x", "compte_csv")}


def test_transformations_regex_template_defaut(tmp_path, srv):
    body = """
        type: http
        url: BASE/json
        auth: {type: none}
        response: {items: results}
        fields:
          url: {template: "https://{id}.exemple.test/p", regex: "^(https://[A-Za-z]+)"}
          title: {template: "{title} ({id})"}
          description: {path: absent, default: "sans description"}
          id: {path: id, regex: "-(\\\\d+)$", prefix: "n"}
    """
    body = body.replace("/json", "/pages")  # /json exige une clé : /pages n'en demande pas
    rep = run_search(make_search(tmp_path, {"t": body}, base=srv.base), ["x"], execute=True)
    first = rep.candidates[0]
    assert first.text == "Page 0 (Page-0). sans description"
    assert first.url == "https://page"  # gabarit puis regex (groupe 1) puis canonicalisation
    assert first.ecosystem == "web" and first.ident == "page"


# --- pagination -------------------------------------------------------------------------------


def paged(pag: str, path: str, extra: str = "") -> str:
    return f"type: http\nurl: BASE/{path}\n{extra}pagination: {pag}\nresponse: {{items: results}}\nfields: {{id: id, title: title}}\n"


def test_pagination_par_numero_de_page(tmp_path, srv):
    cfg = make_search(
        tmp_path,
        {"p": paged("{type: page, param: page, start: 1, max_pages: 5}", "pages")},
        base=srv.base,
    )
    rep = run_search(cfg, ["x"], execute=True)
    assert [r["query"]["page"] for r in srv.log] == ["1", "2", "3", "4"]  # s'arrête à la page vide
    assert len(rep.candidates) == 6


def test_pagination_par_decalage(tmp_path, srv):
    pag = "{type: offset, param: start, size: 2, size_param: count, max_pages: 10}"
    run_search(
        make_search(tmp_path, {"p": paged(pag, "offset")}, base=srv.base), ["x"], execute=True
    )
    assert [r["query"]["start"] for r in srv.log] == ["0", "2", "4", "6"]


def test_pagination_par_curseur(tmp_path, srv):
    pag = "{type: cursor, param: cursor, cursor_path: meta.next, max_pages: 10}"
    run_search(
        make_search(tmp_path, {"p": paged(pag, "cursor")}, base=srv.base), ["x"], execute=True
    )
    assert [r["query"].get("cursor", "") for r in srv.log] == ["", "c2", "c3"]


def test_pagination_lien_suivant_jamais_hors_origine(tmp_path, srv):
    pag = "{type: next_link, next_path: links.next, max_pages: 10}"
    rep = run_search(
        make_search(tmp_path, {"p": paged(pag, "next")}, base=srv.base), ["x"], execute=True
    )
    assert [r["path"] for r in srv.log] == ["/next", "/next"]  # page 2 suivie, lien externe refusé
    assert len(rep.candidates) == 2 and rep.status["p"] == "ok"


def test_limite_de_candidats_respectee(tmp_path, srv):
    cfg = make_search(
        tmp_path,
        {"p": paged("{type: page, param: page, max_pages: 5}", "pages")},
        base=srv.base,
        limite_par_mot_cle=3,
    )
    assert len(run_search(cfg, ["x"], execute=True).candidates) == 3


# --- robustesse : erreurs, quotas, résultats piégés ------------------------------------------


@pytest.mark.parametrize("path", ["429", "403"])
def test_arret_sur_403_et_429(tmp_path, srv, path):
    body = f"type: http\nurl: BASE/{path}\nresponse: {{items: results}}\nfields: {{url: link}}\n"
    rep = run_search(
        make_search(tmp_path, {"p": body}, base=srv.base), ["a", "b", "c"], execute=True
    )
    assert len(srv.log) == 1 and "arrêté" in rep.status["p"]  # pas de nouvelle tentative
    assert path in journal(tmp_path)


@pytest.mark.parametrize("path", ["mal", "500", "redirect", "gros"])
def test_reponses_invalides_contenues_sans_stopper_les_autres(tmp_path, srv, path):
    bad = f"type: http\nurl: BASE/{path}\nresponse: {{items: results}}\nfields: {{url: link}}\n"
    good = (
        "type: http\nurl: BASE/pages\nresponse: {items: results}\nfields: {id: id, title: title}\n"
    )
    cfg = make_search(tmp_path, {"mauvais": bad, "bon": good}, base=srv.base)
    rep = run_search(cfg, ["x"], execute=True)
    assert rep.status["mauvais"].startswith("erreur") and rep.status["bon"] == "ok"
    assert len(rep.candidates) == 2  # le bon fournisseur a répondu quand même


def test_redirection_non_suivie(tmp_path, srv):
    body = "type: http\nurl: BASE/redirect\nresponse: {items: results}\nfields: {url: link}\n"
    run_search(make_search(tmp_path, {"p": body}, base=srv.base), ["x"], execute=True)
    assert [r["path"] for r in srv.log] == ["/redirect"]


def test_quota_par_fournisseur_et_total(tmp_path, srv):
    body = "type: http\nurl: BASE/pages\nquota: 2\nresponse: {items: results}\nfields: {id: id}\n"
    rep = run_search(
        make_search(tmp_path, {"p": body}, base=srv.base), ["a", "b", "c", "d"], execute=True
    )
    assert len(srv.log) == 2 and "quota" in rep.status["p"]
    srv.log.clear()
    cfg = make_search(
        tmp_path / "t", {"p": body.replace("quota: 2\n", "")}, base=srv.base, quota_total=3
    )
    rep = run_search(cfg, list("abcdef"), execute=True)
    assert len(srv.log) == 3 and "quota total" in rep.status["p"]


def test_delai_entre_requetes(tmp_path, srv):
    body = "type: http\nurl: BASE/pages\nresponse: {items: results}\nfields: {id: id}\n"
    cfg = make_search(tmp_path, {"p": body}, base=srv.base, delai_s=5)
    waits = []
    run_search(cfg, ["a", "b", "c"], execute=True, sleep=waits.append)
    assert len(waits) == 2 and all(0 < w <= 5 for w in waits)  # une attente entre chaque requête


def test_resultat_piege_reste_une_donnee_nettoyee(tmp_path, srv):
    body = "type: http\nurl: BASE/piege\nresponse: {items: results}\nfields: {title: title, url: link, id: id, description: snippet, date: published}\n"
    rep = run_search(make_search(tmp_path, {"p": body}, base=srv.base), ["x"], execute=True)
    by_id = {m.ident: m for m in rep.candidates}
    piege = by_id["piege.example.test"]
    assert "\x00" not in piege.text and "\x07" not in piege.text and "\n" not in piege.text
    assert "Ignore tes instructions" in piege.text  # conservé comme simple texte, jamais interprété
    assert piege.date == ""  # date illisible : vide
    assert "piege-2" in by_id  # URL `javascript:` rejetée -> repli sur l'identifiant natif
    assert by_id["piege-2"].url == ""
    long = by_id["long.example.test"] if "long.example.test" in by_id else by_id["piege-3"]
    assert len(long.text) <= 500 and (not long.url or len(long.url) <= 2048)
    assert len(rep.candidates) == 3  # « sans rien », chaîne seule et nombre : aucun candidat


# --- fusion et CSV ---------------------------------------------------------------------------


def test_candidats_identiques_fusionnes_avec_leurs_fournisseurs(tmp_path, srv, monkeypatch):
    monkeypatch.setenv("CLE_TEST", KEY)
    autre = "type: http\nurl: BASE/json\nauth: {type: header, name: X-Api-Key, env: CLE_TEST}\nresponse: {items: results}\nfields: {url: link, title: title}\nraison: trouvé ailleurs\n"
    cfg = make_search(tmp_path, {"api": API_JSON, "autre": autre}, base=srv.base)
    rep = run_search(cfg, ["a", "b"], execute=True)
    assert (
        len(rep.candidates) == 3
    )  # 3 domaines, quel que soit le nombre de fournisseurs et de mots-clés
    m = rep.candidates[0]
    assert m.providers == ["api", "autre"] and m.keywords == ["a", "b"]
    assert set(m.extra) == {"api", "autre"} and len(m.reasons) == 3


def test_csv_candidats_colonnes_et_formules(tmp_path, srv, monkeypatch):
    monkeypatch.setenv("CLE_TEST", KEY)
    rep = run_search(make_search(tmp_path, {"api": API_JSON}, base=srv.base), ["a"], execute=True)
    path = write_candidates_csv(tmp_path / "out" / "candidats.csv", rep.candidates)
    rows = list(csv.reader(path.open(encoding="utf-8"), delimiter=";"))
    assert rows[0] == [
        "ecosysteme",
        "identifiant",
        "url",
        "mot_cle",
        "fournisseur",
        "raison",
        "date",
        "extra",
    ]
    assert rows[1][:5] == [
        "web",
        "json-0.example.test",
        "https://json-0.example.test/page",
        "a",
        "api",
    ]
    # injection de formule tableur : jamais de cellule qui commence par = + - @
    from grappes.search.models import MergedCandidate

    mc = MergedCandidate("web", "=cmd", "=HYPERLINK(1)", ["+x"], ["-p"], ["@r"])
    out = write_candidates_csv(tmp_path / "f.csv", [mc])
    cells = list(csv.reader(out.open(encoding="utf-8"), delimiter=";"))[1]
    assert all(c[:1] not in "=+-@" for c in cells if c)


def test_split_keywords():
    assert split_keywords("soupe, potager\n tomates ,soupe,") == ["soupe", "potager", "tomates"]


# --- client MCP -------------------------------------------------------------------------------

MCP_STDIO = f"""
    type: mcp
    transport: {{type: stdio, command: "{sys.executable}", args: ["{FAKE_MCP}"]}}
    tool: chercher
    arguments: {{query: "{{mots_cles}}", n: "{{limite}}"}}
    response: {{items: resultats}}
    fields: {{title: nom, url: lien, id: id, description: note}}
"""


def test_mcp_dry_run_ne_lance_rien(tmp_path):
    rep = run_search(make_search(tmp_path, {"m": MCP_STDIO}), ["x"])
    assert rep.candidates == [] and "dry-run" in journal(tmp_path)


def test_mcp_stdio_resultats_et_piege(tmp_path):
    rep = run_search(make_search(tmp_path, {"m": MCP_STDIO}), ["x"], execute=True)
    assert rep.status["m"] == "ok"
    by = {m.ident: m for m in rep.candidates}
    assert {"canal_demo_a", "exemple-b.example.test", "piege_1"} <= set(by)
    assert by["canal_demo_a"].ecosystem == "telegram"
    piege = by["piege_1"]
    assert piege.url == "" and "Ignore tes instructions" in piege.text  # donnée, jamais exécutée
    assert "mcp stdio" in journal(tmp_path)


@pytest.mark.parametrize(("tool", "attendu"), [("pas_json", "non JSON"), ("erreur", "erreur")])
def test_mcp_resultats_invalides_contenus(tmp_path, tool, attendu):
    body = MCP_STDIO.replace("tool: chercher", f"tool: {tool}")
    rep = run_search(make_search(tmp_path, {"m": body}), ["x"], execute=True)
    assert rep.candidates == [] and attendu in rep.status["m"]


def test_mcp_commande_introuvable_contenu(tmp_path):
    body = MCP_STDIO.replace(f'"{sys.executable}"', '"/inexistant/serveur"')
    rep = run_search(make_search(tmp_path, {"m": body}), ["x"], execute=True)
    assert rep.candidates == [] and rep.status["m"].startswith("erreur")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mcp_http(tmp_path):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(FAKE_MCP), "http", str(port)],
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        body = MCP_STDIO.replace(
            f'{{type: stdio, command: "{sys.executable}", args: ["{FAKE_MCP}"]}}',
            f'{{type: http, url: "http://127.0.0.1:{port}/mcp"}}',
        )
        rep = run_search(make_search(tmp_path, {"m": body}), ["x"], execute=True)
        assert rep.status["m"] == "ok" and len(rep.candidates) == 3
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# --- ouverture : tout ajout par YAML ou par plugin, sans toucher au cœur ----------------------


def src_digest() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / "src").rglob("*.py")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def test_ouverture_fournisseur_ajoute_par_yaml_seul(tmp_path, srv):
    avant = src_digest()
    body = "type: http\nurl: BASE/pages\nresponse: {items: results}\nfields: {id: id, title: title, url: link}\n"
    cfg = make_search(tmp_path, {"fournisseur_factice_yaml": body}, base=srv.base)
    rep = run_search(cfg, ["x"], execute=True)
    assert len(rep.candidates) == 2 and rep.candidates[0].providers == ["fournisseur_factice_yaml"]
    assert src_digest() == avant  # aucune ligne du cœur n'a changé
    assert not any(
        "fournisseur_factice" in p.read_text("utf-8") for p in (ROOT / "src").rglob("*.py")
    )


def test_ouverture_fournisseur_ajoute_par_plugin_seul(tmp_path):
    avant = src_digest()
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "plugin_factice.py").write_text(
        textwrap.dedent(
            """
            from grappes.search import Candidate, SearchProvider, register_provider


            @register_provider
            class PluginFactice(SearchProvider):
                type = "plugin_factice_test"

                def search(self, keyword, limit):
                    yield Candidate("web", "factice.example.test", "https://factice.example.test/",
                                    keyword, self.id, "plugin de test", extra=dict(self.options))
            """
        ),
        encoding="utf-8",
    )
    (plugins / "casse.py").write_text("raise RuntimeError('plugin défaillant')\n", encoding="utf-8")
    cfg = make_search(tmp_path, {"mon_plugin": "type: plugin_factice_test\nreglage: 7\n"})
    rep = run_search(cfg, ["x"], execute=True)
    assert "plugin_factice_test" in registered_types()
    m = rep.candidates[0]
    assert (m.ident, m.providers, m.extra["mon_plugin"]) == (
        "factice.example.test",
        ["mon_plugin"],
        {"reglage": 7},
    )
    assert src_digest() == avant  # le plugin défaillant est ignoré, le cœur est intact


def test_plugin_qui_plante_ne_stoppe_pas_le_cycle(tmp_path, srv):
    class Plante(SearchProvider):
        type = "plante_test"

        def search(self, keyword, limit):
            raise ZeroDivisionError("bug de plugin")

    register_provider(Plante)
    good = "type: http\nurl: BASE/pages\nresponse: {items: results}\nfields: {id: id}\n"
    cfg = make_search(tmp_path, {"a_plante": "type: plante_test\n", "b_bon": good}, base=srv.base)
    rep = run_search(cfg, ["x"], execute=True)
    assert "ZeroDivisionError" in rep.status["a_plante"] and len(rep.candidates) == 2


def test_type_inconnu_inactif(tmp_path):
    rep = run_search(make_search(tmp_path, {"p": "type: n_existe_pas\n"}), ["x"], execute=True)
    assert "type de fournisseur inconnu" in rep.status["p"]


def test_exemples_de_configuration_se_chargent():
    """Les exemples fournis sont valides ; ils ne sont chargés que s'ils sont copiés dans recherche/."""
    for f in sorted((ROOT / "config" / "recherche" / "exemples").glob("*.yaml")):
        d = yaml.safe_load(f.read_text("utf-8"))
        assert d["id"] and d["type"] in registered_types(), f.name
    assert {
        f.name for f in (ROOT / "config" / "recherche").glob("*.yaml")
    } == {  # les fournisseurs branchés
        "tavily.yaml",
        "rss_franceinfo.yaml",
        "wikipedia.yaml",
        "rss_letudiant.yaml",
        "rss_liberation.yaml",
        # prises de découverte `type: registre` (D68) : lues par decouverte/sources_yaml.py, jamais des fournisseurs actifs
        "bnf_organisations.yaml",
        "esr_etablissements.yaml",
        "culture_festivals.yaml",
        "culture_spectacles_licences.yaml",
    }
    assert set(load_search_config(ROOT / "config").actifs) == {
        "tavily",
        "wikipedia",
        "rss_letudiant",
        "rss_liberation",
        "rss_franceinfo",
    }


# --- liens déjà cités et CLI ------------------------------------------------------------------


@pytest.fixture
def base_liens(tmp_path):
    """Base issue des échantillons de formats (valeurs inventées) : un `run` fait, pour les liens."""
    from grappes.config import load_config
    from grappes.pipeline import ingest, run
    from grappes.storage.db import Database

    cfg = load_config(CONFIG_CLAIR)
    db = Database(tmp_path / "liens.db")
    ech = ROOT / "tests" / "fixtures" / "echantillons"
    for eco, nom in (("x", "x_1.json"), ("telegram", "telegram_1.json"), ("web", "web_1.json")):
        d = tmp_path / eco
        d.mkdir()
        (d / nom).write_bytes((ech / nom).read_bytes())
        ingest(db, d, eco, cfg)
    run(db, cfg)
    return db, tmp_path / "liens.db"


def test_candidats_des_liens_deja_cites(base_liens):
    from grappes.search.internal import internal_candidates

    db, _ = base_liens
    by = {c.key: c for c in internal_candidates(db)}
    assert ("telegram", "canal_demo_b") in by  # cité (transfert, lien), jamais vu comme source
    c = by[("telegram", "canal_demo_b")]
    assert c.provider == "liens_internes" and c.url == "https://t.me/canal_demo_b"
    assert c.extra["citants"] == ["telegram:canal_demo_a"] and c.extra["n_citants"] == 1
    assert ("web", "exemple-a.example.test") not in by  # déjà ingéré comme source
    assert not any(k[1] in ("t.me", "x.com", "t.co") for k in by)


def test_cli_search_dry_run_et_liens_internes(tmp_path, base_liens, capsys, monkeypatch):
    import shutil

    from grappes.cli import main

    _, db_path = base_liens
    cfg = tmp_path / "cfg"
    shutil.copytree(CONFIG_CLAIR, cfg)
    (cfg / "recherche").mkdir(exist_ok=True)
    (cfg / "recherche" / "api.yaml").write_text(
        "id: api\ntype: http\nurl: http://127.0.0.1:9/x\nresponse: {items: r}\nfields: {url: u}\n"
    )
    (cfg / "recherche.yaml").write_text("actifs: [api]\ndelai_s: 0\n")
    out = tmp_path / "sorties"
    code = main(
        [
            "--db",
            str(db_path),
            "--config",
            str(cfg),
            "search",
            "--keywords",
            "soupe,potager",
            "--out",
            str(out),
        ]
    )
    text = capsys.readouterr().out
    assert (
        code == 0
        and "dry-run" in text
        and "api : dry-run" in text
        and "`autonome` est faux" in text
    )
    rows = list(csv.DictReader((out / "candidats.csv").open(encoding="utf-8"), delimiter=";"))
    assert {"telegram"} <= {r["ecosysteme"] for r in rows}
    assert all(r["fournisseur"] == "liens_internes" for r in rows)  # aucun appel externe


def test_cli_search_sans_mot_cle_ni_lien(tmp_path, capsys):
    from grappes.cli import main

    code = main(["--db", str(tmp_path / "absente.db"), "--config", str(CONFIG_CLAIR), "search"])
    assert code == 2 and "aucun mot-clé" in capsys.readouterr().err


def test_cli_search_grappe_inconnue(tmp_path, capsys):
    from grappes.cli import main

    code = main(
        [
            "--db",
            str(tmp_path / "x.db"),
            "--config",
            str(CONFIG_CLAIR),
            "search",
            "--grappe",
            "G-NOPE",
        ]
    )
    assert code == 2 and "grappe inconnue" in capsys.readouterr().err


def test_cli_search_grappe_utilise_son_lexique(tmp_path, capsys):
    from grappes.cli import main

    code = main(
        [
            "--db",
            str(tmp_path / "x.db"),
            "--config",
            str(CONFIG_CLAIR),
            "search",
            "--grappe",
            "G-DEMO-A",
            "--out",
            str(tmp_path / "o"),
        ]
    )
    assert (
        code == 0 and "0 candidats" in capsys.readouterr().out
    )  # aucun fournisseur actif par défaut


# --- interrupteur unique `autonome` et fichier STOP (FINAL.md § 3, § 8) -----------------------


def _cfg_cli(tmp_path, srv, autonomie: str | None):
    import shutil

    cfg = tmp_path / "cfg"
    shutil.copytree(CONFIG_CLAIR, cfg)
    (cfg / "recherche").mkdir(exist_ok=True)
    (cfg / "recherche" / "api.yaml").write_text(
        f"id: api\ntype: http\nurl: {srv.base}/pages\nresponse: {{items: results}}\nfields: {{id: id, title: title}}\n"
    )
    (cfg / "recherche.yaml").write_text("actifs: [api]\ndelai_s: 0\n")
    if autonomie is not None:
        (cfg / "autonomie.yaml").write_text(autonomie)
    return cfg


def test_cli_search_dry_run_tant_que_autonome_est_faux(tmp_path, srv, capsys):
    from grappes.cli import main

    cfg = _cfg_cli(tmp_path, srv, "autonome: false\n")
    code = main(
        [
            "--db",
            str(tmp_path / "x.db"),
            "--config",
            str(cfg),
            "search",
            "--keywords",
            "a",
            "--out",
            str(tmp_path / "o"),
        ]
    )
    assert code == 0 and srv.log == [] and "`autonome` est faux" in capsys.readouterr().out


def test_cli_search_appels_reels_seulement_si_autonome(tmp_path, srv, capsys):
    from grappes.cli import main

    cfg = _cfg_cli(tmp_path, srv, "autonome: true\n")
    code = main(
        [
            "--db",
            str(tmp_path / "x.db"),
            "--config",
            str(cfg),
            "search",
            "--keywords",
            "a",
            "--out",
            str(tmp_path / "o"),
        ]
    )
    assert code == 0 and len(srv.log) == 1 and "appels réels" in capsys.readouterr().out


def test_cli_search_fichier_stop_arrete_tout(tmp_path, srv, capsys):
    from grappes.cli import main

    stop = tmp_path / "STOP"
    stop.write_text("")
    cfg = _cfg_cli(tmp_path, srv, f"autonome: true\nstop_file: {stop}\n")
    code = main(["--db", str(tmp_path / "x.db"), "--config", str(cfg), "search", "--keywords", "a"])
    assert code == 4 and srv.log == [] and "arrêt demandé" in capsys.readouterr().err
