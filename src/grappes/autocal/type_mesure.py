"""Mesure du vérificateur de type (D69, point 2) sur un jeu (mise au point ou validation, jamais le test brûlé).

Pour chaque sujet : les candidats ajoutés par les prises YAML sont passés au vérificateur ; on compte les écartés par
prise et par motif, les acteurs de la référence perdus (jamais « devinés » : ils sont jugés comme à l'accoutumée), et la
R-précision avant/après. Rien n'est activé par ce module ; l'archive des prises est enrichie des champs de type
(requêtes en cache, aucun appel réseau nouveau).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

from grappes.autocal import evaluation as ev
from grappes.autocal import protocole as pro
from grappes.autocal.sources_mesure import decouverts_yaml
from grappes.autocal.test_final import params_figes
from grappes.decouverte.traduction import charger_naf
from grappes.decouverte.verification_type import demande_type, filtrer


def sans_yaml_ctx(d, sans_yaml):
    """Contexte sans candidat de prise (calcul des candidats de base) : même objet, découvertes remplacées."""
    d.ctx.decouverts = sans_yaml
    return d.ctx


def main(jeu: str = "dev") -> int:
    if jeu == "test":
        print("REFUSÉ : le jeu test est brûlé (D67).", file=sys.stderr)
        return 2
    naf = charger_naf("config/naf_rev2.csv")
    base = params_figes()
    res: dict = {}
    for d in pro.charger_jeu(jeu, ner=True):
        if d.ref is None or d.juge is None:
            continue
        nouveaux, _ = decouverts_yaml(d.id, d.sujet.phrase, refaire=True)  # enrichit l'archive des champs de type
        gardes, ecartes = filtrer(nouveaux, demande_type(d.sujet.phrase), naf)
        ids = tuple(sorted({x.registre for x in nouveaux}))
        sans_yaml = [x for x in d.ctx.decouverts if x.registre != "yaml"]
        prm = replace(base, registres=(*base.registres, *ids))
        out: dict = {"phrase": d.sujet.phrase, "R": d.ref.r}
        classements: dict[str, dict] = {}
        for nom, liste in (("avant", nouveaux), ("apres", gardes)):
            d.ctx.decouverts = sans_yaml + liste
            jug = ev.juger_classement(d.juge, ev.classer(d.ctx, prm))
            m = ev.mesurer(d.ref, jug)
            docs: dict[str, str] = {}
            for j in jug:
                docs.setdefault(j.docid, j.verdict)
            classements[nom] = {j.classe.ref: j.verdict for j in jug}
            ju = {k for k, v in docs.items() if v == "juste"}
            out[nom] = {"justes": len(ju), "candidats": len(jug), "Rprec": m["mesures"]["Rprec"], "bpref": m["mesures"]["bpref"], "ids_justes": sorted(ju)}
        d.ctx.decouverts = sans_yaml
        n0 = len(ev.classer(d.ctx, replace(base, registres=(*base.registres, *ids))))
        out["candidats_base"] = n0
        out["ajoutes"] = {"avant": out["avant"]["candidats"] - n0, "apres": out["apres"]["candidats"] - n0}
        out["perdus"] = sorted(set(out["avant"]["ids_justes"]) - set(out["apres"]["ids_justes"]))
        retires = set(classements["avant"]) - set(classements["apres"])
        out["retires_verdicts"] = dict(Counter(classements["avant"][r] for r in retires))
        out["ajoutes_par_prise"] = {}
        for i in ids:
            r_i = {}
            for nom, liste in (("avant", nouveaux), ("apres", gardes)):
                d.ctx.decouverts = sans_yaml + [x for x in liste if x.registre == i]
                r_i[nom] = len(ev.classer(d.ctx, replace(base, registres=(*base.registres, i)))) - len(ev.classer(sans_yaml_ctx(d, sans_yaml), replace(base, registres=(*base.registres, i))))
            out["ajoutes_par_prise"][i] = r_i
        d.ctx.decouverts = sans_yaml + nouveaux
        par_prise = {}
        for i in ids:
            tot = [x for x in nouveaux if x.registre == i]
            ec = [(x, m) for x, m in ecartes if x.registre == i]
            par_prise[i] = {"candidats": len(tot), "ecartes": len(ec), "inconnus": sum(1 for x in tot if not x.type_candidat and not x.nature)}
        out["par_prise"] = par_prise
        out["motifs"] = Counter(m.split(" sans mot")[0] if m.startswith("type") else m for _, m in ecartes).most_common(6)
        out["exemples_ecartes"] = [f"{x.nom} [{x.registre}] : {m}" for x, m in ecartes[:4]]
        res[d.id] = out
        d.juge.save()
        print(d.id, json.dumps({k: v for k, v in out.items() if k not in ("avant", "apres", "exemples_ecartes")}, ensure_ascii=False), flush=True)
        print("   ajoutés", out["ajoutes"], "retirés jugés", out["retires_verdicts"], "avant", {k: out["avant"][k] for k in ("justes", "Rprec")}, "après", {k: out["apres"][k] for k in ("justes", "Rprec")}, flush=True)
    Path(f"autocal/resultats/type_{jeu}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:] or ["dev"])))
