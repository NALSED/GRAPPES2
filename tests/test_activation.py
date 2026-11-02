"""Activation : formats de configuration de Sednal, secrets, fournisseur Tavily (faux serveur local)."""

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from fakes_recherche import FakeServer
from grappes.autonomy import load_autonomy, load_subjects
from grappes.config import ConfigError
from grappes.env import load_secrets
from grappes.search.config import load_search_config
from grappes.search.engine import run_search

ROOT = Path(__file__).resolve().parents[1]


# --- formats de configuration fournis par Sednal ----------------------------------------------------


def test_autonomie_forme_courte(tmp_path):
    (tmp_path / "autonomie.yaml").write_text(
        "quotas:\n  requetes_par_cycle: 20\n  nouvelles_sources_par_cycle: 10\nprofondeur_max: 2\n"
        "interdits:\n  - reddit.com\n  - facebook.com\n  - contenu adulte\nautonome: true\n",
        encoding="utf-8",
    )
    a = load_autonomy(tmp_path)
    assert a.autonome and (a.requetes_par_cycle, a.nouvelles_sources_par_cycle, a.profondeur) == (
        20,
        10,
        2,
    )
    assert a.interdits_domaines == ("reddit.com", "facebook.com") and a.interdits_motifs == (
        "contenu adulte",
    )
    assert (
        a.pages_par_cycle == 100 and a.delai_domaine_s == 1.0
    )  # le reste : valeurs par défaut prudentes


def test_autonomie_forme_longue_inchangee(tmp_path):
    (tmp_path / "autonomie.yaml").write_text(
        "requetes_par_cycle: 7\ninterdits: {domaines: [a.test], motifs: [x]}\n", "utf-8"
    )
    a = load_autonomy(tmp_path)
    assert (
        a.requetes_par_cycle == 7
        and a.interdits_domaines == ("a.test",)
        and a.interdits_motifs == ("x",)
    )


def test_autonomie_invalide_toujours_refusee(tmp_path):
    (tmp_path / "autonomie.yaml").write_text("quotas: 5\n", "utf-8")
    with pytest.raises(ConfigError):
        load_autonomy(tmp_path)
    (tmp_path / "autonomie.yaml").write_text("interdits: [1, 2]\n", "utf-8")
    with pytest.raises(ConfigError):
        load_autonomy(tmp_path)


def test_sujet_nom_et_phrase(tmp_path):
    (tmp_path / "sujets.yaml").write_text(
        'sujets:\n  - nom: actualite_fr\n    phrase: "Les acteurs de l\'actualité française en ce moment"\n  - "Une autre phrase"\n',
        encoding="utf-8",
    )
    s = load_subjects(tmp_path).subjects
    assert (
        s[0].id == "actualite_fr"
        and s[0].phrase.startswith("Les acteurs")
        and s[1].id.startswith("sujet-")
    )
    (tmp_path / "sujets.yaml").write_text(
        "sujets:\n  - nom: mauvais nom!\n    phrase: x\n", "utf-8"
    )
    with pytest.raises(ConfigError):
        load_subjects(tmp_path)
    (tmp_path / "sujets.yaml").write_text("sujets:\n  - nom: seul\n", "utf-8")
    with pytest.raises(ConfigError):
        load_subjects(tmp_path)


def test_fichiers_de_sednal_du_depot_se_chargent():
    assert load_autonomy(ROOT / "config").autonome in (True, False)
    assert load_subjects(ROOT / "config").subjects


# --- secrets ----------------------------------------------------------------------------------------------


def test_secrets_charges_sans_ecraser_ni_retourner_les_valeurs(tmp_path, monkeypatch):
    f = tmp_path / "secrets.env"
    f.write_text(
        '# commentaire\nCLE_A=valeur-a\nexport CLE_B="valeur b"\nVIDE=\nCLE_C = c\nmauvaise ligne\n',
        "utf-8",
    )
    f.chmod(0o600)
    for n in ("CLE_A", "CLE_B", "CLE_C", "VIDE"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("CLE_C", "deja-definie")
    noms = load_secrets(f)
    assert sorted(noms) == ["CLE_A", "CLE_B"]  # on ne rend que des noms, jamais des valeurs
    assert os.environ["CLE_A"] == "valeur-a" and os.environ["CLE_B"] == "valeur b"
    assert os.environ["CLE_C"] == "deja-definie" and "VIDE" not in os.environ  # rien n'est écrasé
    for n in ("CLE_A", "CLE_B"):
        monkeypatch.delenv(n)


def test_secrets_fichier_absent_ou_trop_ouvert(tmp_path, capsys):
    assert load_secrets(tmp_path / "absent.env") == []
    f = tmp_path / "s.env"
    f.write_text("X_JAMAIS_DEFINIE_1=1\n", "utf-8")
    f.chmod(0o644)
    load_secrets(f)
    err = capsys.readouterr().err
    assert "chmod 600" in err and "X_JAMAIS_DEFINIE_1=1" not in err  # jamais la valeur
    os.environ.pop("X_JAMAIS_DEFINIE_1", None)
    assert stat.S_IMODE((ROOT / ".gitignore").stat().st_mode) is not None
    assert "config/secrets.env" in (ROOT / ".gitignore").read_text("utf-8")


# --- Tavily dans la multiprise ------------------------------------------------------------------------------


class FakeTavily(FakeServer):
    pass


def _tavily_vers(base: str, tmp_path: Path):
    """Charge le vrai config/recherche/tavily.yaml en ne remplaçant que l'URL par le faux serveur."""
    cfg = tmp_path / "config"
    (cfg / "recherche").mkdir(parents=True)
    spec = yaml.safe_load((ROOT / "config" / "recherche" / "tavily.yaml").read_text("utf-8"))
    spec["url"] = f"{base}/tavily"
    (cfg / "recherche" / "tavily.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), "utf-8"
    )
    (cfg / "recherche.yaml").write_text(
        "actifs: [tavily]\ndelai_s: 0\nlimite_par_mot_cle: 10\n", "utf-8"
    )
    return load_search_config(cfg)


def test_tavily_est_branche_dans_le_depot():
    scfg = load_search_config(ROOT / "config")
    assert "tavily" in scfg.actifs
    spec = scfg.providers["tavily"]
    assert (
        spec.type == "http"
        and spec.public
        and spec.options["url"] == "https://api.tavily.com/search"
    )
    assert spec.options["method"] == "POST" and spec.options["auth"] == {
        "type": "bearer",
        "env": "TAVILY_API_KEY",
    }
    assert "tvly-" not in (ROOT / "config" / "recherche" / "tavily.yaml").read_text(
        "utf-8"
    )  # jamais de clé en dur


def test_requete_tavily_conforme_a_la_documentation(tmp_path, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-cle-de-test-0123456789")
    with FakeServer() as srv:
        rep = run_search(
            _tavily_vers(srv.base, tmp_path), ["élections locales"], execute=True, sujet="actualité"
        )
    (req,) = srv.log
    assert req["method"] == "POST" and req["path"] == "/tavily"
    assert req["headers"]["Authorization"] == "Bearer tvly-cle-de-test-0123456789"
    body = json.loads(req["body"])
    assert (
        body["query"] == "élections locales"
        and body["topic"] == "news"
        and body["search_depth"] == "basic"
    )
    assert body["max_results"] == 10 and isinstance(
        body["max_results"], int
    )  # entier, comme l'exige l'API
    assert body["include_answer"] is False and body["include_raw_content"] is False
    assert rep.status["tavily"] == "ok"


def test_reponse_tavily_convertie_en_candidats(tmp_path, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-cle-de-test-0123456789")
    with FakeServer() as srv:
        rep = run_search(_tavily_vers(srv.base, tmp_path), ["élections"], execute=True)
    assert rep.raw and all(c.provider == "tavily" and c.url.startswith("https://") for c in rep.raw)
    c = rep.raw[0]
    assert c.text and c.ecosystem == "web" and c.date == "2026-09-01"  # published_date -> date
    assert c.extra.get("score") is not None  # champs propres à Tavily conservés dans `extra`


def test_cle_tavily_jamais_dans_le_journal_et_absente_inactive(tmp_path, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-cle-de-test-0123456789")
    scfg = None
    with FakeServer() as srv:
        scfg = _tavily_vers(srv.base, tmp_path)
        run_search(scfg, ["a"], execute=True)
    journal = (tmp_path / "donnees" / "journal_recherche.log").read_text("utf-8")
    assert "tvly-cle-de-test" not in journal and "POST" in journal and "tavily" in journal
    monkeypatch.delenv("TAVILY_API_KEY")
    with FakeServer() as srv:
        scfg = _tavily_vers(srv.base, tmp_path / "b")
        rep = run_search(scfg, ["a"], execute=True)
        assert srv.log == [] and "clé absente" in rep.status["tavily"]  # inactif, aucun appel


def test_garde_fou_aucune_requete_reelle_dans_les_tests():
    """Le garde-fou de conftest : un hôte non local échoue bruyamment, sans sortir de la machine."""
    from grappes.search.http import urllib_transport

    with pytest.raises(AssertionError, match="requête réelle interdite"):
        urllib_transport("GET", "https://api.tavily.com/search", {}, None, 5.0, 1000)
    assert "TAVILY_API_KEY" not in os.environ  # la vraie clé n'est jamais visible des tests


# --- leçons du premier cycle réel ---------------------------------------------------------------------------


def test_reseaux_sociaux_et_plateformes_jamais_lus_ni_cites_comme_sites():
    from grappes.autonomy import DEFAULT_SOCIAL
    from grappes.canon import link_target

    for hote in (
        "bsky.app",
        "youtube.com",
        "tiktok.com",
        "linkedin.com",
        "facebook.com",
        "reddit.com",
    ):
        assert hote in DEFAULT_SOCIAL  # le récupérateur ne les lit jamais
        assert (
            link_target(f"https://www.{hote}/une/page") == ""
        )  # et un lien vers eux ne crée aucune entité web
    assert (
        link_target("https://x.com/compte") == "x:compte"
        and link_target("https://lemonde.fr/a") == "web:lemonde.fr"
    )


def test_lien_isole_ne_classe_pas_une_entite_jamais_lue():
    """Un pied de page qui cite un site ne dit rien de ce site : son contexte ne compte qu'à partir de
    deux sources citantes distinctes."""
    import tempfile

    from grappes.clustering.assign import assign
    from test_phase4 import STRONG, make_cfg, obs

    cfg = make_cfg(Path(tempfile.mkdtemp()), seuil_min=30, contexte_min_citants=2)
    seul = [obs("web:page1.test", STRONG, links=["https://pied-de-page.test/"])]
    res = assign(seul, cfg)
    ref = {e.primary_ref: e.entity_id for e in res.entities}["web:pied-de-page.test"]
    assert not [m for m in res.memberships if m["entity_id"] == ref and m["confidence"] > 0]
    deux = [*seul, obs("web:page2.test", STRONG, links=["https://pied-de-page.test/"])]
    res2 = assign(deux, cfg)
    ref2 = {e.primary_ref: e.entity_id for e in res2.entities}["web:pied-de-page.test"]
    assert [m for m in res2.memberships if m["entity_id"] == ref2 and m["confidence"] > 0]


def test_la_configuration_reelle_ne_contient_aucun_jeu_d_essai():
    assert (
        not list((ROOT / "config" / "grappes").glob("*.yaml"))
        if (ROOT / "config" / "grappes").exists()
        else True
    )


def test_par_defaut_un_site_jamais_lu_n_est_jamais_classe_d_apres_ses_citants():
    """Réglage de base (D45) : sans texte propre, seul le recoupement classe une entité (google.com cité
    par deux pages d'actualité ne devient pas « probable »)."""
    import tempfile

    from grappes.clustering.assign import assign
    from test_phase4 import STRONG, make_cfg, obs

    cfg = make_cfg(Path(tempfile.mkdtemp()), seuil_min=30)
    assert cfg.scoring.contexte_min_citants is None
    o = [obs(f"web:page{i}.test", STRONG, links=["https://google.com/"]) for i in range(3)]
    res = assign(o, cfg)
    ref = {e.primary_ref: e.entity_id for e in res.entities}["web:google.com"]
    assert not [m for m in res.memberships if m["entity_id"] == ref and m["confidence"] > 0]
