"""Ouverture UNIQUE du jeu de test (D62/D66) : paramètres figés, mesure des critères 1 à 8 de `docs/CERTIFICATION.md`.

`python -m grappes.autocal.test_final` ouvre le jeu test. Le journal `autocal/test_ouvert.json` est créé AVANT toute
lecture du test (création exclusive) ; une seconde exécution est refusée et consignée dans `reouvertures` : le test est
« brûlé » et le verdict doit le dire.
`python -m grappes.autocal.test_final --essai validation` joue exactement la même mesure sur un autre jeu (répétition
de mise au point du code) : ni journal, ni toucher au jeu test.

Système mesuré : paramètres d'`autocal/resultats/etape3_optuna.json`, registre SIRENE, noms lus, Wikidata désactivé,
variantes désactivées. Probabilité calibrée = sigmoïde de Platt figée dans `config/calibration.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from grappes.autocal import controles as ct
from grappes.autocal import etape3
from grappes.autocal import evaluation as ev
from grappes.autocal import protocole as pro
from grappes.autocal.jeux import DonneesSujet
from grappes.autocal.juger import FAUX, JUSTE

JOURNAL = Path("autocal/test_ouvert.json")
RESULTATS = Path("autocal/resultats")
TAU = 0.90
N_PERM = 1000
GRAINE_PERM = 62
FIGES = (
    "config/lr.yaml", "config/ner.yaml", "config/classement.yaml", "config/calibration.json",
    "autocal/resultats/etape3_optuna.json", "autocal/jeux.json", "docs/CERTIFICATION.md",
)  # fmt: skip


def _sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _git_head() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()


def params_figes() -> ev.Params:
    best = json.loads(Path("autocal/resultats/etape3_optuna.json").read_text(encoding="utf-8"))["params"]
    return etape3.params_depuis_essai(best)


def platt() -> tuple[float, float]:
    d = json.loads(Path("config/calibration.json").read_text(encoding="utf-8"))["ajustement_platt_non_valide"]
    return float(d["a"]), float(d["b"])


def proba(lg: float, ab: tuple[float, float]) -> float:
    return 1.0 / (1.0 + math.exp(-(ab[0] * lg + ab[1])))


def _avec(prm: ev.Params, **kw: float) -> ev.Params:
    d = dict(prm.lr)
    d.update(kw)
    return replace(prm, lr=tuple(d.items()))


def comparaisons(prm: ev.Params) -> dict[str, ev.Params]:
    return {
        "complet (réglé, sans Wikidata)": prm,
        "sans SIRENE/RNA": replace(prm, registres=()),
        "sans noms lus": replace(prm, ner=False),
        "sans proximité du texte propre": _avec(prm, proximite_pente=0.0),
        "sans écosystèmes": _avec(prm, ecosysteme=0.0),
        "sans citations": _avec(prm, citation=0.0),
        "web seul (aucun fournisseur)": replace(prm, registres=(), ner=False),
        "système d'avant (actuel)": ev.Params(methode="actuel"),
        "vote majoritaire": ev.Params(methode="vote", w_s=0.5, w_t=0.5),
    }


def mesurer_sujet(d: DonneesSujet, prm: ev.Params, ab: tuple[float, float]) -> dict:
    rows = ev.classer(d.ctx, prm)
    ps = [proba(r.croyance, ab) for r in rows]
    out: dict = {
        "id": d.id, "phrase": d.sujet.phrase, "difficulte": d.sujet.difficulte, "candidats": len(rows),
        "p_max": max(ps, default=0.0), "n_p_tau": sum(1 for p in ps if p >= TAU),
        "sirene_total": len({x.siren or x.identifiant for x in d.ctx.decouverts if x.registre == "sirene"}),
    }  # fmt: skip
    if d.ref is None or d.juge is None:  # témoin : aucun acteur réel
        out["temoin"] = True
        return out
    jug = ev.juger_classement(d.juge, rows)
    m = ev.mesurer(d.ref, jug)
    r = m["R"]
    out.update(
        {
            "R": r, "Rprec": m["mesures"]["Rprec"], "Rprec_ex_aequo": m["mesures"]["Rprec_ex_aequo"],
            "bpref": m["mesures"]["bpref"], "infAP": m["mesures"]["infAP"], "map": m["mesures"]["map"],
            "comptes": m["comptes"], "comptes_R": m["comptes_R"],
            "faux_p_tau": sum(1 for j, p in zip(jug, ps, strict=True) if j.verdict == FAUX and p >= TAU),
            "rendus_si_N_egal_R": min(r, len(rows)),
            "remplissage": {str(n): min(n, len(rows)) / n for n in (20, 50, 100, 200)},
        }
    )  # fmt: skip
    run = ev.run_de(d.id, jug)[d.id]
    docids = [k for k, _ in sorted(run.items(), key=lambda kv: (-kv[1], kv[0]))]
    justes = {j.docid for j in jug if j.verdict == JUSTE} | {e.qid for e in d.ref.entities}
    out["permutation"] = ct.permutation(d.id, docids, justes, r, N_PERM, GRAINE_PERM)
    out["rang"] = [
        {"nom": j.classe.nom, "ref": j.classe.ref, "log10_lr": round(j.classe.croyance, 6), "p": round(p, 6), "verdict": j.verdict}
        for j, p in zip(jug[:200], ps[:200], strict=True)
    ]  # fmt: skip
    return out


def mesurer_jeu(jeu: str) -> dict:
    """Mesure complète d'un jeu (chargement frais depuis les archives : zéro collecte). Tout est déterministe."""
    prm, ab = params_figes(), platt()
    donnees = pro.charger_jeu(jeu, ner=True)
    sujets = [mesurer_sujet(d, prm, ab) for d in donnees]
    x: list[float] = []
    y: list[int] = []
    for d in donnees:
        if d.ref is None or d.juge is None:
            continue
        xs, ys, _ = etape3.echantillon([d], prm)
        x += xs
        y += ys
    pr = [proba(v, ab) for v in x]
    comp = {}
    for nom, p in comparaisons(prm).items():
        v, w, b = [], [], []
        for d in donnees:
            if d.ref is None or d.juge is None:
                continue
            m = ev.mesurer(d.ref, ev.juger_classement(d.juge, ev.classer(d.ctx, p)))["mesures"]
            v.append(m["Rprec"]), w.append(m["Rprec_ex_aequo"]), b.append(m["bpref"])
        comp[nom] = {"Rprec": sum(v) / len(v), "Rprec_ex_aequo": sum(w) / len(w), "bpref": sum(b) / len(b)}
    for d in donnees:
        if d.juge:
            d.juge.save()
    return {
        "jeu": jeu, "sujets": sujets, "calibration": {
            "n": len(x), "positifs": int(sum(y)), "ece": ct.ece(pr, y), "brier": ct.brier(pr, y),
            "brier_constant": ct.brier_base(y), "p_max": max(pr, default=0.0), "x": x, "y": y, "p": pr,
        }, "comparaisons": comp,
    }  # fmt: skip


def ligne_b(sujets: list[dict], jeu: str) -> dict:
    """Sujets à liste fermée par définition (`autocal/ligne_b.json`, figé avant ouverture). `sirene_total` (T) est donné
    à titre d'information seulement."""
    ids = set(json.loads(Path("autocal/ligne_b.json").read_text(encoding="utf-8"))["jeux"].get(jeu, []))
    return {
        s["id"]: {"R": s["R"], "total_sirene_info": s["sirene_total"], "ferme": s["id"] in ids}
        for s in sujets
        if not s.get("temoin")
    }


def critere_1(sujets: list[dict], ids: list[str]) -> dict:
    v = [s for s in sujets if s["id"] in ids]
    moy = sum(s["Rprec"] for s in v) / len(v) if v else 0.0
    return {"valeur": moy, "bpref": sum(s["bpref"] for s in v) / len(v) if v else 0.0, "seuil": 0.90, "ok": bool(v) and moy >= 0.90, "sujets": ids}


def verdict(res: dict, pytest_vert: bool, identiques: bool, optimisme: dict) -> dict:
    pos = [s for s in res["sujets"] if not s.get("temoin")]
    tem = [s for s in res["sujets"] if s.get("temoin")]
    idsA = [s["id"] for s in pos]
    fermes = [i for i, v in res["ligne_b"].items() if v["ferme"]]
    c2s = [s for s in pos if 20 <= s["R"] <= 200]
    ok2 = [0.9 * s["R"] <= s["rendus_si_N_egal_R"] <= 1.1 * s["R"] for s in c2s]
    c2 = {
        "taux": sum(ok2) / len(ok2) if ok2 else 0.0, "ok": bool(ok2) and sum(ok2) / len(ok2) >= 0.8, "seuil": 0.8,
        "remplissage": {n: sum(s["remplissage"][n] for s in pos) / len(pos) for n in ("20", "50", "100", "200")},
        "par_sujet": {s["id"]: [s["R"], s["rendus_si_N_egal_R"]] for s in c2s},
    }  # fmt: skip
    c4a = {"p_max_temoin": max((s["p_max"] for s in tem), default=0.0), "ok": all(s["n_p_tau"] == 0 for s in tem)}
    c4b = {"faux_a_p_tau": sum(s["faux_p_tau"] for s in pos), "ok": all(s["faux_p_tau"] == 0 for s in pos)}
    perm_moy = sum(s["permutation"]["permute_moyen"] for s in pos) / len(pos)
    hasard = sum(s["R"] / max(1, s["candidats"]) for s in pos) / len(pos)
    depasse = [s["permutation"]["observe"] > s["permutation"]["permute_p95"] for s in pos]
    c4c = {
        "permute_moyen": perm_moy, "hasard_R_sur_n": hasard, "borne": hasard + 0.05, "part_depassant_p95": sum(depasse) / len(pos),
        "ok": perm_moy <= hasard + 0.05 and sum(depasse) / len(pos) >= 0.8,
    }  # fmt: skip
    cal = res["calibration"]
    c5 = {
        "ece": cal["ece"], "brier": cal["brier"], "brier_constant": cal["brier_constant"],
        "ok": cal["ece"] <= 0.10 and cal["brier"] < cal["brier_constant"],
    }  # fmt: skip
    communs = {
        "2": c2, "3": {"ok": True, "note": "rapporté à part, sans seuil"}, "4a": c4a, "4b": c4b, "4c": c4c, "5": c5,
        "6": {"ok": identiques}, "7": {"ok": pytest_vert}, "8": {"ok": True, **optimisme},
    }
    base_ok = all(v["ok"] for v in communs.values())
    A = {"1": critere_1(res["sujets"], idsA), **communs}
    B = (
        {"1": critere_1(res["sujets"], fermes), **communs}
        if fermes
        else {"1": {"ok": False, "valeur": None, "note": "non évaluable : aucun sujet à liste fermée (R/T >= 0,5)"}, **communs}
    )
    return {
        "ligne_A": {"criteres": A, "certifie": A["1"]["ok"] and base_ok},
        "ligne_B": {"criteres": B, "certifie": bool(fermes) and B["1"]["ok"] and base_ok, "evaluable": bool(fermes), "sujets_fermes": fermes},
    }  # fmt: skip


def lancer(jeu: str, essai: bool) -> dict:
    """Mesure deux fois (critère 6), pytest (critère 7), verdict. `jeu` vaut « test » à l'ouverture."""
    r1 = mesurer_jeu(jeu)
    dossier = Path("donnees/autocal")
    dossier.mkdir(parents=True, exist_ok=True)
    f1, f2 = dossier / f"{jeu}_rejeu1.json", dossier / f"{jeu}_rejeu2.json"
    f1.write_text(json.dumps(r1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    r2 = mesurer_jeu(jeu)
    f2.write_text(json.dumps(r2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    identiques = f1.read_bytes() == f2.read_bytes()
    r1["ligne_b"] = ligne_b(r1["sujets"], jeu)
    val = mesurer_jeu("validation") if jeu == "test" else None
    optimisme = {}
    if val is not None:
        pos = [s for s in r1["sujets"] if not s.get("temoin")]
        pv = [s for s in val["sujets"] if not s.get("temoin")]
        for k in ("Rprec", "Rprec_ex_aequo"):
            mv, mt = sum(s[k] for s in pv) / len(pv), sum(s[k] for s in pos) / len(pos)
            optimisme[k] = {"validation": mv, "test": mt, "ecart": mv - mt}
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], capture_output=True, text=True)
    r1["pytest"] = (p.stdout.strip().splitlines() or [""])[-1]
    r1["verdict"] = verdict(r1, p.returncode == 0, identiques, optimisme)
    r1["rejeux_identiques_octet_pour_octet"] = identiques
    r1["empreinte_rejeu"] = hashlib.sha256(f1.read_bytes()).hexdigest()[:16]
    cal = r1["calibration"]
    ct.diagramme(
        "docs/conception/fiabilite_test.png" if jeu == "test" else f"donnees/autocal/fiabilite_{jeu}.png",
        {"p calibrée (Platt figée)": (cal["p"], cal["y"])}, f"Fiabilité, jeu {jeu} (étiquette stricte)",
    )  # fmt: skip
    for s in r1["sujets"]:
        s.pop("rang", None)  # les classements restent dans donnees/autocal/<jeu>_rejeu1.json (hors dépôt)
    for k in ("x", "y", "p"):
        cal.pop(k, None)
    sortie = RESULTATS / ("test_final.json" if jeu == "test" else f"essai_{jeu}.json")
    sortie.write_text(json.dumps(r1, indent=1, ensure_ascii=False), encoding="utf-8")
    return r1


def ouvrir() -> int:
    maintenant = datetime.now(UTC).isoformat(timespec="seconds")
    if JOURNAL.exists():
        j = json.loads(JOURNAL.read_text(encoding="utf-8"))
        j.setdefault("reouvertures", []).append(maintenant)
        JOURNAL.write_text(json.dumps(j, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"REFUSÉ : le jeu test a déjà été ouvert le {j['ouvert_le_utc']} ; tentative consignée, le test est brûlé.", file=sys.stderr)
        return 2
    entree = {
        "ouvert_le_utc": maintenant, "commit": _git_head(), "statut": "en cours",
        "empreintes": {f: _sha(f) for f in FIGES}, "jeu": "test", "reouvertures": [],
    }  # fmt: skip
    with open(JOURNAL, "x", encoding="utf-8") as f:  # création exclusive : jamais deux ouvertures simultanées
        json.dump(entree, f, indent=1, ensure_ascii=False)
    try:
        res = lancer("test", essai=False)
    except BaseException as exc:
        entree["statut"] = f"interrompu : {type(exc).__name__}: {exc}"
        JOURNAL.write_text(json.dumps(entree, indent=1, ensure_ascii=False), encoding="utf-8")
        raise
    entree["statut"] = "terminé"
    entree["fini_le_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    entree["empreinte_rejeu"] = res["empreinte_rejeu"]
    JOURNAL.write_text(json.dumps(entree, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="test_final")
    ap.add_argument("--essai", choices=["dev", "validation"], help="répétition sur un autre jeu, sans journal")
    a = ap.parse_args(argv)
    if a.essai:
        res = lancer(a.essai, essai=True)
        print(json.dumps(res["verdict"], indent=1, ensure_ascii=False)[:6000])
        return 0
    return ouvrir()


if __name__ == "__main__":
    sys.exit(main())
