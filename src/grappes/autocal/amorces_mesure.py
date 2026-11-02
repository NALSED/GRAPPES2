"""Mesure, SANS RIEN ACTIVER, du gain qu'apporteraient 3 exemples fournis en entrée (D69, point 3).

Définition TREC Entity (2010-2011) : le système reçoit 2 à 23 entités connues comme exemples de départ. Ici : 3 acteurs
de la référence, tirés au sort (graine fixe, 5 tirages par sujet), ENLEVÉS de la référence et du classement avant de
mesurer (on compte les entités nouvelles, comme TREC).

Mécanisme mesuré : le type d'activité des exemples dans le registre SIRENE. Pour chaque exemple, on cherche son nom
dans l'annuaire des entreprises ; les codes NAF trouvés (les plus fréquents) remplacent le code NAF que la traduction de
la phrase ne trouve presque jamais (`code_naf` rend None sur les 12 sujets) ; la découverte SIRENE habituelle par code
NAF + mots du complément s'applique alors. Wikidata n'est PAS utilisé : la référence en dérive, le gain serait fictif.

Mesuré, par tirage : acteurs de la référence (hors exemples) parmi les candidats, candidats, R-précision et bpref,
avec et sans exemples ; exemples retrouvés dans le registre ; exemples déjà présents parmi les candidats ; coût réseau.
Le jeu test est brûlé : ce module refuse de le charger.
"""

from __future__ import annotations

import json
import random
import sys
import urllib.parse
from collections import Counter
from dataclasses import replace
from pathlib import Path

from grappes.autocal import evaluation as ev
from grappes.autocal import protocole as pro
from grappes.autocal.sources_mesure import CACHE, decouverts_yaml
from grappes.autocal.test_final import params_figes
from grappes.classement.fusion import norm_name
from grappes.decouverte.client import DecouverteError, Http
from grappes.decouverte.registres import API_ENTREPRISES, DecouverteConfig, sirene
from grappes.decouverte.traduction import CodeNaf, charger_naf, mots, noyau, racine
from grappes.decouverte.verification_type import demande_type, filtrer

GRAINE = 20261012
TIRAGES = 5
EXEMPLES = 3


def tirer(sujet_id: str, entites: list, k: int) -> list:
    rng = random.Random(f"{GRAINE}:{sujet_id}:{k}")
    return rng.sample(sorted(entites, key=lambda e: e.qid), EXEMPLES)


def naf_de(nom: str, http: Http) -> str:
    """Code NAF de l'unité SIRENE dont le nom est celui de l'exemple (égalité de noms normalisés, sinon tous les mots
    de l'exemple dans le nom du registre). Vide si le registre ne le connaît pas : on ne devine pas."""
    url = API_ENTREPRISES + "?" + urllib.parse.urlencode({"q": nom, "per_page": "10", "etat_administratif": "A"})
    try:
        res = http.get_json(url).get("results", [])  # type: ignore[union-attr]
    except DecouverteError:
        return ""
    cible = norm_name(nom)
    mc = set(cible.split())
    for r in res:
        n = norm_name(r.get("nom_complet") or "")
        if n == cible or (mc and mc <= set(n.split()) and len(mc) >= 2):
            return r.get("activite_principale") or (r.get("siege") or {}).get("activite_principale") or ""
    return ""


def codes_retenus(codes: list[str]) -> list[str]:
    """Les codes les plus fréquents parmi ceux que les exemples ont donnés (égalité : tous les ex æquo)."""
    c = Counter(x for x in codes if x)
    if not c:
        return []
    top = max(c.values())
    return sorted(k for k, v in c.items() if v == top)


def decouverte_par_exemples(phrase: str, codes: list[str], naf: dict[str, str], http: Http) -> list:
    out = []
    for code in codes:
        lib = naf.get(code, "")
        lm = {racine(m) for m in mots(lib)}
        reste = [m for m in noyau(phrase) if racine(m) not in lm]
        out += sirene(phrase, CodeNaf(code, lib, 1.0, reste), http, DecouverteConfig(sources=("sirene",)))
    return out


def _mesure(d, prm, ref, retirer: set[str]) -> dict:
    jug = [j for j in ev.juger_classement(d.juge, ev.classer(d.ctx, prm)) if j.docid not in retirer]
    m = ev.mesurer(ref, jug)
    ju = {j.docid for j in jug if j.verdict == "juste"}
    return {"justes": len(ju), "candidats": len({j.docid for j in jug}), "Rprec": m["mesures"]["Rprec"], "bpref": m["mesures"]["bpref"], "ids": ju}


def main(jeu: str = "dev") -> int:
    if jeu == "test":
        print("REFUSÉ : le jeu test est brûlé (D67).", file=sys.stderr)
        return 2
    naf_liste = charger_naf("config/naf_rev2.csv")
    naf = dict(naf_liste)
    base = params_figes()
    http = Http(CACHE, delai_s=1.0)
    res: dict = {}
    for d in pro.charger_jeu(jeu, ner=True):
        if d.ref is None or d.juge is None:
            continue
        nouveaux, _ = decouverts_yaml(d.id, d.sujet.phrase)
        gardes, _ = filtrer(nouveaux, demande_type(d.sujet.phrase), naf_liste)
        ids = tuple(sorted({x.registre for x in nouveaux}))
        prm = replace(base, registres=(*base.registres, *ids))
        sans = [x for x in d.ctx.decouverts if x.registre != "yaml"]
        tirages = []
        for k in range(TIRAGES):
            ex = tirer(d.id, d.ref.entities, k)
            qex = {e.qid for e in ex}
            ref = replace(d.ref, entities=[e for e in d.ref.entities if e.qid not in qex])
            a0 = http.appels_reseau
            codes = [naf_de(e.label, http) for e in ex]
            retenus = codes_retenus(codes)
            ajout = decouverte_par_exemples(d.sujet.phrase, retenus, naf, http) if retenus else []
            reseau = http.appels_reseau - a0
            d.ctx.decouverts = sans + gardes
            avant = _mesure(d, prm, ref, qex)
            d.ctx.decouverts = sans + gardes + ajout
            apres = _mesure(d, prm, ref, qex)
            d.ctx.decouverts = sans + gardes
            presents = ev.juger_classement(d.juge, ev.classer(d.ctx, prm))
            ex_presents = len({j.docid for j in presents if j.docid in qex})
            tirages.append({
                "exemples": [e.label for e in ex], "codes_trouves": codes, "codes_retenus": retenus, "ajout_registre": len(ajout),
                "exemples_deja_candidats": ex_presents, "reseau": reseau,
                "avant": {k_: v for k_, v in avant.items() if k_ != "ids"}, "apres": {k_: v for k_, v in apres.items() if k_ != "ids"},
                "gain_justes": len(apres["ids"] - avant["ids"]),
            })  # fmt: skip
            d.juge.save()
            print(d.id, k, json.dumps({x: tirages[-1][x] for x in ("codes_retenus", "ajout_registre", "exemples_deja_candidats", "gain_justes", "avant", "apres")}, ensure_ascii=False), flush=True)
        res[d.id] = {"phrase": d.sujet.phrase, "R": d.ref.r, "tirages": tirages}
    Path(f"autocal/resultats/amorces_{jeu}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(*(sys.argv[1:] or ["dev"])))
