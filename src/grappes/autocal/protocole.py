"""Protocole de mesure : mesures de base, étude Optuna, validation imbriquée, jeu de test réservé.

Tout rejoue des données archivées (aucun réseau, hors Wikidata pour juger un nom jamais vu, mis en cache).
"""

from __future__ import annotations

import json
from pathlib import Path

from grappes.autocal import evaluation as ev
from grappes.autocal.jeux import DonneesSujet, charger_sujet, sujets_du_jeu
from grappes.autocal.wikidata import Wikidata, WikidataError

RESULTATS = Path("autocal/resultats")
METHODES = ("actuel", "vote", "sums", "averagelog", "investment", "pooledinvestment", "truthfinder")


def prejuger(d: DonneesSujet) -> int:
    """Juge TOUS les candidats du sujet (une fois ; verdicts en cache) : les essais d'optimisation n'interrogent
    plus Wikidata. Retourne le nombre de noms jugés."""
    if d.juge is None:
        return 0
    n = 0
    for c in d.ctx.candidats:
        d.juge.juger(c.nom or c.nom_brut, c.ref)
        n += 1
        if n % 10 == 0:
            d.juge.save()
    d.juge.save()
    return n


def charger_jeu(jeu: str, juger: bool = True, w: Wikidata | None = None, ner: bool = False, ids: set[str] | None = None) -> list[DonneesSujet]:
    w = w or Wikidata("donnees/autocal/wikidata")
    out = []
    for s in sujets_du_jeu(jeu):
        if ids is not None and s.id not in ids:
            continue
        if not (Path("donnees/autocal/sujets") / s.id / "collecte.txt").exists():
            print(f"{s.id} : pas encore collecté, ignoré")
            continue
        d = charger_sujet(s, w, juger, ner)
        if juger:
            try:
                prejuger(d)
            except WikidataError as exc:
                print(f"{s.id} : jugement incomplet ({exc})")
        out.append(d)
    return out


def tableau_base(donnees: list[DonneesSujet]) -> dict:
    """Chaque méthode de classement, réglages neutres (w_s = w_t = 0,5), mesurée sur chaque sujet."""
    res: dict = {}
    for d in donnees:
        if d.ref is None or d.juge is None:
            continue
        res[d.id] = {"R": d.ref.r, "candidats": len(d.ctx.candidats), "difficulte": d.sujet.difficulte}
        for m in METHODES:
            p = ev.Params(methode=m, w_s=1.0 if m == "actuel" else 0.5, w_t=0.0 if m == "actuel" else 0.5)
            r = ev.mesurer(d.ref, ev.juger_classement(d.juge, ev.classer(d.ctx, p)))
            res[d.id][m] = {
                "Rprec": r["mesures"]["Rprec"],
                "bpref": r["mesures"]["bpref"],
                "infAP": r["mesures"]["infAP"],
                "n": r["n"],
                "comptes": r["comptes"],
            }
    return res


def afficher(res: dict) -> None:
    print(f"{'sujet':16} {'diff':9} {'R':>4} {'cand':>5} " + " ".join(f"{m[:9]:>10}" for m in METHODES))
    for sid, r in res.items():
        print(
            f"{sid:16} {r['difficulte']:9} {r['R']:4d} {r['candidats']:5d} "
            + " ".join(f"{r[m]['Rprec']:10.3f}" for m in METHODES)
        )
    n = len(res) or 1
    print(f"{'moyenne':16} {'':9} {'':>4} {'':>5} " + " ".join(f"{sum(r[m]['Rprec'] for r in res.values()) / n:10.3f}" for m in METHODES))


def main(jeu: str) -> int:
    donnees = charger_jeu(jeu)
    res = tableau_base(donnees)
    afficher(res)
    RESULTATS.mkdir(parents=True, exist_ok=True)
    (RESULTATS / f"base_{jeu}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0
