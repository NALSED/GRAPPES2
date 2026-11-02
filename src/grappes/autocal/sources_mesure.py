"""Mesure des prises YAML (D68, chantier A) sur le jeu de mise au point SEULEMENT : combien d'acteurs de la référence
arrivent en plus parmi les candidats, et à quel coût (appels réseau, volume, temps, candidats ajoutés dont faux).

Les résultats de chaque prise sont archivés par sujet (`donnees/autocal/decouverte_yaml/<sujet>.json`) : un rejeu ne
retourne pas au réseau. Le jeu test est brûlé : ce module refuse de le charger.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from grappes.autocal import etape3
from grappes.autocal import evaluation as ev
from grappes.autocal import protocole as pro
from grappes.autocal.juger import FAUX, JUSTE, NON_VERIFIABLE
from grappes.autocal.test_final import params_figes
from grappes.decouverte.client import Http
from grappes.decouverte.registres import Decouvert
from grappes.decouverte.sources_yaml import charger_sources, interroger

ARCHIVE = Path("donnees/autocal/decouverte_yaml")
CACHE = Path("donnees/autocal/decouverte_cache")


def decouverts_yaml(sujet_id: str, phrase: str, ids: tuple[str, ...] = (), refaire: bool = False) -> tuple[list[Decouvert], dict]:
    """Découverte par les prises YAML (archivée) et son coût : {id: {n, secondes, appels_reseau}}."""
    f = ARCHIVE / f"{sujet_id}.json"
    if f.exists() and not refaire:
        d = json.loads(f.read_text(encoding="utf-8"))
        return [Decouvert(**x) for x in d["decouverts"]], d["cout"]
    http = Http(CACHE, delai_s=1.0)
    out: list[Decouvert] = []
    cout: dict = {}
    for s in charger_sources():
        if not s.actif or (ids and s.id not in ids):
            continue
        a0, t0 = http.appels_reseau, time.time()
        r = interroger(s, phrase, http)
        out += r
        cout[s.id] = {"n": len(r), "secondes": round(time.time() - t0, 1), "appels_reseau": http.appels_reseau - a0}
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"decouverts": [asdict(x) for x in out], "cout": cout}, ensure_ascii=False, indent=1), encoding="utf-8")
    return out, cout


def _justes(d, prm) -> tuple[set[str], int, dict[str, int]]:
    jug = ev.juger_classement(d.juge, ev.classer(d.ctx, prm))
    docs: dict[str, str] = {}
    for j in jug:
        docs.setdefault(j.docid, j.verdict)
    return {k for k, v in docs.items() if v == JUSTE}, len(jug), {v: sum(1 for x in docs.values() if x == v) for v in (JUSTE, FAUX, NON_VERIFIABLE)}


def main(jeu: str = "dev") -> int:
    if jeu == "test":
        print("REFUSÉ : le jeu test est brûlé (D67).", file=sys.stderr)
        return 2
    from dataclasses import replace

    base = params_figes()
    data = pro.charger_jeu(jeu, ner=True)
    res: dict = {}
    for d in data:
        if d.ref is None or d.juge is None:
            continue
        nouveaux, cout = decouverts_yaml(d.id, d.sujet.phrase)
        ids = tuple(sorted({x.registre for x in nouveaux}))
        j0, n0, c0 = _justes(d, base)
        r: dict = {"R": d.ref.r, "base": {"justes": len(j0), "candidats": n0, "comptes": c0}, "sources": {}, "cout": cout}
        d.ctx.decouverts = [x for x in d.ctx.decouverts if x.registre != "yaml"] + nouveaux
        for i in ids:
            ji, ni, ci = _justes(d, replace(base, registres=(*base.registres, i)))
            r["sources"][i] = {"justes": len(ji), "en_plus": sorted(ji - j0), "candidats": ni, "ajoutes": ni - n0, "comptes": ci}
        if ids:
            jt, nt, ct_ = _justes(d, replace(base, registres=(*base.registres, *ids)))
            prm_m = ev.mesurer(d.ref, ev.juger_classement(d.juge, ev.classer(d.ctx, replace(base, registres=(*base.registres, *ids)))))
            r["toutes"] = {"justes": len(jt), "en_plus": len(jt - j0), "candidats": nt, "ajoutes": nt - n0, "comptes": ct_, "Rprec": prm_m["mesures"]["Rprec"], "bpref": prm_m["mesures"]["bpref"]}
        res[d.id] = r
        if d.juge:
            d.juge.save()
        print(d.id, json.dumps({k: v for k, v in r.items() if k != "sources"}, ensure_ascii=False), flush=True)
    Path(f"autocal/resultats/sources_{jeu}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:] or ["dev"])))
