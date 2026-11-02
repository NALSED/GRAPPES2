"""Collecte d'un sujet d'évaluation : UNE collecte réelle enregistrée en cassette, puis rejeu illimité sans réseau.

Chaque sujet vit dans `donnees/autocal/sujets/<id>/` : `config/` (copie de la configuration, fournisseurs de
l'évaluation seulement), `cassette.sqlite` (toutes les réponses réseau), `grappes.db` (base du rejeu courant).
Non-circularité (DECISION_AUTOCAL.md, méthode 2) : ni Wikipédia ni Wikidata ni flux dérivés ne sont des
fournisseurs de l'évaluation ; seul Tavily (recherche web générale) l'est. Les registres officiels (SIRENE, RNA,
ESR) ne dérivent pas de Wikidata : ils restent actifs.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import yaml

from grappes.autocal.cassette import Cassette, Recorder, Replayer
from grappes.cycle.engine import CycleOutcome, run_cycle

RACINE = Path("donnees/autocal/sujets")
FOURNISSEURS = ("tavily_general",)  # fournisseurs de l'évaluation (hors wikipedia, wikidata, flux)

TAVILY_GENERAL = """\
# Tavily, recherche web générale (évaluation autocal) : aucune restriction d'actualité. 1 crédit par requête.
id: tavily_general
type: http
public: true
url: https://api.tavily.com/search
method: POST
auth: {type: bearer, env: TAVILY_API_KEY}
body:
  type: json
  content:
    query: "{mots_cles}"
    topic: general
    search_depth: basic
    max_results: "{limite}"
    include_answer: false
    include_raw_content: false
    include_images: false
    exclude_domains: [wikipedia.org, wikidata.org, wikimedia.org, wikiwand.com, dbpedia.org]
response: {format: json, items: results}
fields:
  title: title
  url: url
  description: content
  id: id
quota: 20
raison: "résultat Tavily (général) pour « {mot_cle} »"
"""

# Plafonds de collecte d'un sujet d'évaluation (le volume demandé en dépend). Une requête = 1 crédit Tavily.
# Dérivés de la source de la référence : jamais collectés (non-circularité, méthode 2).
DERIVES_WIKIDATA = ("wikipedia.org", "wikidata.org", "wikimedia.org", "wikiwand.com", "dbpedia.org")

PLAFONDS = {
    "requetes_par_cycle": 8,
    "pages_par_cycle": 200,
    "nouvelles_sources_par_cycle": 200,
    "elements_par_cycle": 600,
    "profondeur": 2,
    "requetes_termes": 12,  # phrase + 6 paires de termes = 7 requêtes, plus 1 pour l'amorçage
}


def dossier(sujet_id: str, racine: Path = RACINE) -> Path:
    return racine / sujet_id


def preparer(
    sujet_id: str, phrase: str, base_config: str | Path = "config", racine: Path = RACINE
) -> Path:
    """Crée le dossier du sujet et sa configuration isolée. Idempotent : ne touche pas à la cassette."""
    d = dossier(sujet_id, racine)
    cfg = d / "config_initiale"  # jamais modifiée : le rejeu repart toujours de là
    if cfg.exists():
        shutil.rmtree(cfg)
    shutil.copytree(
        base_config, cfg,
        ignore=shutil.ignore_patterns("secrets.env", "apprentissage", "recherche", "exemples"),
    )  # fmt: skip
    (cfg / "recherche").mkdir()
    (cfg / "recherche" / "tavily_general.yaml").write_text(TAVILY_GENERAL, encoding="utf-8")
    (cfg / "apprentissage").mkdir()
    (cfg / "sujets.yaml").write_text(
        yaml.safe_dump({"sujets": [{"nom": sujet_id, "phrase": phrase}]}, allow_unicode=True),
        encoding="utf-8",
    )
    rech = yaml.safe_load((Path(base_config) / "recherche.yaml").read_text(encoding="utf-8"))
    rech.update(
        actifs=list(FOURNISSEURS), quota_total=PLAFONDS["requetes_par_cycle"],
        limite_par_mot_cle=20, delai_s=0.5, journal=str(d / "journal_recherche.log"),
    )  # fmt: skip
    (cfg / "recherche.yaml").write_text(yaml.safe_dump(rech, allow_unicode=True), encoding="utf-8")
    auto = yaml.safe_load((Path(base_config) / "autonomie.yaml").read_text(encoding="utf-8"))
    auto.pop("sujets", None)
    auto["rapports_dir"] = str(d / "rapports")
    auto.update(PLAFONDS, autonome=True, stop_file=str(d / "STOP"), seuil_ingestion=20)
    interdits = auto.get("interdits") or []
    auto["interdits"] = list(dict.fromkeys([*interdits, *DERIVES_WIKIDATA]))
    auto["quotas"] = {
        "requetes_par_cycle": PLAFONDS["requetes_par_cycle"],
        "nouvelles_sources_par_cycle": PLAFONDS["nouvelles_sources_par_cycle"],
    }
    (cfg / "autonomie.yaml").write_text(yaml.safe_dump(auto, allow_unicode=True), encoding="utf-8")
    shutil.rmtree(d / "config", ignore_errors=True)
    shutil.copytree(
        cfg, d / "config"
    )  # copie de travail, modifiée par le cycle (amorçage, termes appris)
    return d


def collecter(sujet_id: str, racine: Path = RACINE, now: datetime | None = None) -> CycleOutcome:
    """Collecte RÉELLE (réseau, quota Tavily) : un cycle complet, toutes les réponses gardées en cassette."""
    from grappes.env import load_secrets

    load_secrets("config/secrets.env")
    d = dossier(sujet_id, racine)
    (d / "donnees").mkdir(parents=True, exist_ok=True)
    cass = Cassette(d / "cassette.sqlite")
    rec = Recorder(cass)
    when = now or datetime.now(UTC)
    (d / "date_collecte.txt").write_text(when.isoformat(), encoding="utf-8")
    out = run_cycle(
        d / "config", d / "grappes.db", d / "sorties", d / "donnees",
        now=when, search_transport=rec, fetch_transport=rec,
    )  # fmt: skip
    (d / "collecte.txt").write_text(
        f"statut={out.status} requetes_reseau={rec.network_calls} reponses={len(cass)}\n",
        encoding="utf-8",
    )
    cass.close()
    return out


def rejouer_collecte(
    sujet_id: str,
    cible: Path,
    surcharges: dict[str, dict] | None = None,
    racine: Path = RACINE,
) -> tuple[CycleOutcome, Replayer]:
    """Rejoue la collecte depuis la cassette dans `cible` (base neuve, ZÉRO réseau). `surcharges` : fichier de
    configuration -> clés à fusionner (ex. {"autonomie.yaml": {"seuil_ingestion": 15}})."""
    d = dossier(sujet_id, racine)
    cible = Path(cible)
    if cible.exists():
        shutil.rmtree(cible)
    shutil.copytree(d / "config_initiale", cible / "config")
    for name, patch in (surcharges or {}).items():
        p = cible / "config" / name
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        data.update(patch)
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    os.environ.setdefault(
        "TAVILY_API_KEY", "rejeu-sans-reseau"
    )  # la clé n'entre pas dans la clé de cassette
    cass = Cassette(d / "cassette.sqlite")
    rep = Replayer(cass)
    when = datetime.fromisoformat((d / "date_collecte.txt").read_text(encoding="utf-8"))
    (cible / "donnees").mkdir()
    out = run_cycle(
        cible / "config", cible / "grappes.db", cible / "sorties", cible / "donnees",
        now=when, search_transport=rep, fetch_transport=rep, sleep=lambda _s: None,
    )  # fmt: skip
    cass.close()
    return out, rep
