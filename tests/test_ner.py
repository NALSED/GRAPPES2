"""Reconnaissance de noms (D63, chantier 2) : découpage, nettoyage, agrégation, doublons. Aucun modèle chargé."""

from pathlib import Path

from grappes.ner import extraire as ex


def test_morceaux_couvrent_tout_le_texte():
    t = ("Phrase numéro un. " * 200).strip()
    ms = ex.morceaux(t, 300)
    assert "".join(m for _, m in ms) == t
    assert all(len(m) <= 300 for _, m in ms)
    assert all(t[d : d + len(m)] == m for d, m in ms)


def test_nettoyer_et_retenir():
    cfg = ex.NerConfig()
    assert ex.nettoyer("  les Girondins de Bordeaux, ") == "Girondins de Bordeaux"
    assert ex.retenir("Metz Handball", cfg)
    assert not ex.retenir("handball", cfg)  # aucune majuscule
    assert not ex.retenir("12", cfg)
    assert not ex.retenir(" ".join(["Mot"] * 9), cfg)


def test_recoller_apostrophe():
    t = "OGC Nice Côte d'Azur Handball"
    ms = [ex.Mention(0, 15, 0.9, "m"), ex.Mention(16, 29, 0.95, "m")]
    r = ex._recoller(ms, t)
    assert len(r) == 1 and t[r[0].debut : r[0].fin] == t and r[0].score == 0.9


def test_variantes_signalees():
    noms = {k: ex.Nomme(k, k) for k in ("fenix toulouse", "fenix toulouse handball", "metz handball")}
    assert ex.variantes(noms) == [("fenix toulouse", "fenix toulouse handball")]


def test_mentions_cache_et_seuil(tmp_path: Path, monkeypatch):
    m = ex.ModeleNer("m", "faux/modele", categories={"ORG": ["ORG"]})
    cfg = ex.NerConfig((m,), seuil_confiance=0.8)
    appels = []

    class Pipe:
        def __call__(self, morceau):
            appels.append(1)
            return [
                {"entity_group": "ORG", "start": 0, "end": 5, "score": 0.9},
                {"entity_group": "ORG", "start": 6, "end": 9, "score": 0.5},
                {"entity_group": "PER", "start": 10, "end": 13, "score": 0.99},
                {"entity_group": "ORG", "start": 90, "end": 99, "score": 0.99},  # hors texte : écarté
            ]

    monkeypatch.setattr(ex, "_pipeline", lambda *_: Pipe())
    texte = "Metz Handball joue"
    r = ex.mentions_modele(texte, m, cfg, tmp_path)
    assert [(x.debut, x.fin) for x in r] == [(0, 5)]
    r2 = ex.mentions_modele(texte, m, cfg, tmp_path)  # cache : le modèle ne retourne pas
    assert r2 == r and len(appels) == 1
