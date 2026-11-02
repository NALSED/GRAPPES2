"""Configuration commune des tests.

* Aucune requête réelle, jamais : le transport HTTP par défaut échoue bruyamment pour tout hôte autre
  que la boucle locale (faux serveurs). Les tests ne voient ni la clé Tavily ni la configuration
  d'activation de Sednal (`autonome: true`, `secrets.env`, sujets, fournisseurs actifs).
* `CONFIG_CLAIR` : copie de `config/` sans ces fichiers d'activation et sans masquage des personnes
  (le défaut du dépôt est `redact_persons: true`, D30), pour les tests qui vérifient des identifiants.
"""

import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
# fichiers d'activation, et pondération Admiralty (les tests historiques supposent des poids uniformes ; les
# tests de l'Admiralty fournissent leur propre table)
ACTIVATION = (
    "secrets.env",
    "autonomie.yaml",
    "sujets.yaml",
    "recherche.yaml",
    "tavily.yaml",
    "fiabilite_sources.yaml",
)


def _copie_claire() -> Path:
    dest = Path(tempfile.mkdtemp(prefix="grappes_config_clair_")) / "config"
    shutil.copytree(
        ROOT / "config", dest, ignore=shutil.ignore_patterns(*ACTIVATION, "apprentissage")
    )
    # jeux d'essai (2 grappes de démonstration) : hors de la configuration réelle, copiés ici seulement
    demo = ROOT / "tests" / "fixtures" / "config_demo" / "grappes"
    shutil.copytree(demo, dest / "grappes", dirs_exist_ok=True)
    path = dest / "scoring.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("redact_persons: true", "redact_persons: false"),
        encoding="utf-8",
    )
    return dest


CONFIG_CLAIR = _copie_claire()
LOCAL = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def _aucune_requete_reelle(monkeypatch):
    """Garde-fou : toute tentative de sortie vers un hôte non local fait échouer le test."""
    from grappes.fetch import pages as pages_mod
    from grappes.search import http as http_mod

    reel = http_mod.urllib_transport

    def garde(method, url, headers, body, timeout, max_bytes):
        host = (urlsplit(url).hostname or "").casefold()
        if host not in LOCAL:
            raise AssertionError(f"requête réelle interdite dans les tests : {method} {host}")
        return reel(method, url, headers, body, timeout, max_bytes)

    monkeypatch.setattr(http_mod, "urllib_transport", garde)
    monkeypatch.setattr(pages_mod, "urllib_transport", garde)
    for nom in ("TAVILY_API_KEY",):
        monkeypatch.delenv(nom, raising=False)
