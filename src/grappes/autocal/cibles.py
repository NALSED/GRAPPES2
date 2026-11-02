"""R-précision par cible (D69, point 1) : cible A (acteurs d'un registre officiel, >= 0,90) et cible B (pas de liste
faisant autorité, 0,30 à 0,40 = état de l'art mondial), rendues SÉPARÉMENT, jamais fusionnées.

Classification des sujets : `autocal/cibles.json`, figée avant toute mesure, sur la seule phrase. Trois configurations,
sur un jeu de mise au point ou de validation (le jeu test est brûlé et refusé) :
* `fige` : le système certifié en D67 (SIRENE, noms lus, Wikidata désactivé), sans prise YAML ;
* `prises` : + les prises data.gouv.fr (D68), sans vérificateur de type ;
* `prises_type` : + les prises passées au vérificateur de type (D69, point 2).
Les témoins (R = 0) n'ont pas de cible. R-précision stricte : un non vérifiable n'est jamais compté juste.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from statistics import mean

from grappes.autocal import evaluation as ev
from grappes.autocal import protocole as pro
from grappes.autocal.sources_mesure import decouverts_yaml
from grappes.autocal.test_final import params_figes
from grappes.decouverte.traduction import charger_naf
from grappes.decouverte.verification_type import demande_type, filtrer

CIBLES = Path("autocal/cibles.json")
SEUILS = {"A": 0.90, "B": (0.30, 0.40)}


def cible_de(sujet_id: str, jeu: str) -> str:
    return json.loads(CIBLES.read_text(encoding="utf-8"))["sujets"][jeu][sujet_id]


def main(jeu: str = "dev") -> int:
    if jeu == "test":
        print("REFUSÉ : le jeu test est brûlé (D67).", file=sys.stderr)
        return 2
    naf = charger_naf("config/naf_rev2.csv")
    base = params_figes()
    res: dict = {"sujets": {}}
    for d in pro.charger_jeu(jeu, ner=True):
        c = cible_de(d.id, jeu)
        if d.ref is None or d.juge is None or c == "temoin":
            continue
        nouveaux, _ = decouverts_yaml(d.id, d.sujet.phrase)
        gardes, _ = filtrer(nouveaux, demande_type(d.sujet.phrase), naf)
        ids = tuple(sorted({x.registre for x in nouveaux}))
        sans = [x for x in d.ctx.decouverts if x.registre != "yaml"]
        r: dict = {"cible": c, "R": d.ref.r, "phrase": d.sujet.phrase}
        for nom, liste, prm in (
            ("fige", [], base),
            ("prises", nouveaux, replace(base, registres=(*base.registres, *ids))),
            ("prises_type", gardes, replace(base, registres=(*base.registres, *ids))),
        ):
            d.ctx.decouverts = sans + liste
            jug = ev.juger_classement(d.juge, ev.classer(d.ctx, prm))
            m = ev.mesurer(d.ref, jug)
            ju = {j.docid for j in jug if j.verdict == "juste"}
            r[nom] = {"Rprec": m["mesures"]["Rprec"], "justes_parmi_candidats": len(ju), "candidats": len({j.docid for j in jug})}
        d.juge.save()
        res["sujets"][d.id] = r
        print(d.id, c, json.dumps({k: r[k] for k in ("fige", "prises", "prises_type")}), flush=True)
    res["moyennes"] = {
        c: {k: round(mean(s[k]["Rprec"] for s in res["sujets"].values() if s["cible"] == c), 4) for k in ("fige", "prises", "prises_type")}
        | {"sujets": sum(1 for s in res["sujets"].values() if s["cible"] == c), "seuil": SEUILS[c]}
        for c in ("A", "B")
        if any(s["cible"] == c for s in res["sujets"].values())
    }
    print(json.dumps(res["moyennes"], ensure_ascii=False))
    Path(f"autocal/resultats/cibles_{jeu}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:] or ["dev"])))
