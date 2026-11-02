"""Mesure du lien des variantes (D65, calibration.md § 4) : fusion désactivée, « simple » ou « complet », sur des données ARCHIVÉES (zéro requête).

Usage : `python -m grappes.autocal.mesure_lien [dev|validation] [seuil ...]`. Le jeu TEST est refusé (ouvert une seule fois, brûlé). Rien n'est réglé ici :
les paramètres figés de l'étape 3 sont repris tels quels, seul le regroupement des variantes change. Même rejeu, mêmes jugements que `test_final`."""

from __future__ import annotations

import sys
from dataclasses import replace

from grappes.autocal import evaluation as ev
from grappes.autocal import protocole as pro
from grappes.autocal.test_final import params_figes


def configurations(prm: ev.Params, seuils: list[float]) -> dict[str, ev.Params]:
    out = {"sans fusion": replace(prm, variantes=False)}
    for s in seuils:
        out[f"simple {s:g}"] = replace(prm, variantes=True, seuil_variantes=s, lien="simple")
        out[f"complet {s:g}"] = replace(prm, variantes=True, seuil_variantes=s, lien="complet")
    return out


def mesurer(jeu: str, seuils: list[float]) -> list[dict]:
    if jeu == "test":
        raise SystemExit("REFUSÉ : le jeu test est brûlé, il ne se rejoue pas ici.")
    donnees = [d for d in pro.charger_jeu(jeu, ner=True) if d.ref is not None and d.juge is not None]
    rows = []
    for nom, p in configurations(params_figes(), seuils).items():
        v, w, b, cand, justes = [], [], [], 0, 0
        for d in donnees:
            classes = ev.classer(d.ctx, p)
            m = ev.mesurer(d.ref, ev.juger_classement(d.juge, classes))
            v.append(m["mesures"]["Rprec"]), w.append(m["mesures"]["Rprec_ex_aequo"]), b.append(m["mesures"]["bpref"])
            cand += len(classes)
            justes += m["comptes"].get("juste", 0)
        n = len(donnees)
        rows.append({"config": nom, "Rprec": sum(v) / n, "Rprec_ex_aequo": sum(w) / n, "bpref": sum(b) / n, "candidats": cand, "justes": justes, "sujets": n})
    for d in donnees:
        d.juge.save()
    return rows


def main() -> None:
    jeu = sys.argv[1] if len(sys.argv) > 1 else "dev"
    seuils = [float(x) for x in sys.argv[2:]] or [0.9]
    print(f"jeu {jeu} — seuils {seuils}")
    print("| configuration | sujets | candidats | justes | Rprec | Rprec ex æquo | bpref |\n|---|---|---|---|---|---|---|")
    for r in mesurer(jeu, seuils):
        print(f"| {r['config']} | {r['sujets']} | {r['candidats']} | {r['justes']} | {r['Rprec']:.4f} | {r['Rprec_ex_aequo']:.4f} | {r['bpref']:.4f} |")


if __name__ == "__main__":
    main()
