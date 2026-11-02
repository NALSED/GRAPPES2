"""Étape 3 (D64) : réglage de l'indice de fiabilité (rapport de vraisemblance) sur le jeu de mise au point, vérification
sur le jeu de validation, puis calibration. Tout se rejoue depuis les archives (zéro réseau, Wikidata jugé en cache).

Objectif d'Optuna : R-précision stricte moyenne (mesure principale) ; la précision moyenne (MAP) y est ajoutée à
poids 0,01 UNIQUEMENT pour départager les essais quand la R-précision est plate (beaucoup de sujets à 0). Les
valeurs de départ (config/lr.yaml) sont le raisonnement d'expert ; Optuna les ajuste dans des bornes fixées d'avance.
"""

from __future__ import annotations

import optuna

from grappes.autocal import evaluation as ev
from grappes.autocal import optimiser
from grappes.autocal.jeux import DonneesSujet

# (borne basse, borne haute) fixées avant les résultats ; Wikidata n'est pas réglable (non mesurable ici).
BORNES = {
    "registre_sirene_naf": (0.0, 3.0),
    "registre_sirene_nom": (0.0, 3.0),
    "nom_dans_texte": (0.0, 2.0),
    "nom_dans_texte_plafond": (0.5, 3.0),
    "proximite_pente": (0.0, 4.0),
    "proximite_pivot": (0.0, 1.0),
    "ecosysteme": (0.0, 2.0),
    "citation": (0.0, 1.0),
    "citation_plafond": (0.0, 3.0),
}


def params_lr(trial: optuna.Trial, registres=("sirene",), ner=True) -> ev.Params:
    lr = tuple((k, trial.suggest_float(k, lo, hi)) for k, (lo, hi) in BORNES.items())
    return ev.Params(
        methode="lr", registres=registres, ner=ner, lr=lr,
        variantes=trial.suggest_categorical("variantes", [True, False]),
        seuil_variantes=trial.suggest_categorical("seuil_variantes", [0.7, 0.8, 0.9, 0.95]),  # valeurs discrètes : regroupements mémorisés
    )  # fmt: skip


def objectif(donnees: list[DonneesSujet], p: ev.Params) -> float:
    r, _ = optimiser.score_jeu(donnees, p, "Rprec_ex_aequo")
    m, _ = optimiser.score_jeu(donnees, p, "map")
    return r + 0.01 * m


def etude(nom: str, donnees: list[DonneesSujet], essais: int, registres=("sirene",), ner=True, patience: int = 60):
    return optimiser.etude(
        nom, lambda t: objectif(donnees, params_lr(t, registres, ner)), essais, graine=62, patience=patience
    )


def params_depuis_essai(best: dict, registres=("sirene",), ner=True) -> ev.Params:
    lr = tuple((k, best[k]) for k in BORNES)
    return ev.Params(methode="lr", registres=registres, ner=ner, lr=lr, variantes=best["variantes"], seuil_variantes=best["seuil_variantes"])


def echantillon(donnees: list[DonneesSujet], p: ev.Params) -> tuple[list[float], list[int], list[str]]:
    """(log10 LR, étiquette stricte juste=1 / sinon 0, sujet) de tous les candidats classés des sujets positifs."""
    x: list[float] = []
    y: list[int] = []
    s: list[str] = []
    for d in donnees:
        if d.ref is None or d.juge is None:
            continue
        for j in ev.juger_classement(d.juge, ev.classer(d.ctx, p)):
            x.append(j.classe.croyance)
            y.append(1 if j.verdict == "juste" else 0)
            s.append(d.id)
    return x, y, s


def main(essais: int = 150) -> None:
    """Étude sur dev (sans Wikidata), puis mesure des meilleurs paramètres sur dev ET validation, comparées aux
    paramètres d'expert (config/lr.yaml) : écart d'optimisme = dev − validation. Écrit autocal/resultats/etape3_optuna.json."""
    import json
    from pathlib import Path

    from grappes.autocal import protocole as p

    dev = p.charger_jeu("dev", ner=True)
    val = p.charger_jeu("validation", ner=True)
    expert = ev.Params(methode="lr", registres=("sirene",), ner=True)
    st = etude("etape3-dev-v1", dev, essais)
    best = params_depuis_essai(st.best_params)

    def mesures(donnees, prm):
        _, det = optimiser.score_jeu(donnees, prm, "Rprec_ex_aequo")
        return {k: v["mesures"]["Rprec_ex_aequo"] for k, v in det.items()}

    out = {
        "essais": len(st.trials), "meilleur_objectif_dev": st.best_value, "params": st.best_params,
        "expert": {"dev": mesures(dev, expert), "validation": mesures(val, expert)},
        "optimise": {"dev": mesures(dev, best), "validation": mesures(val, best)},
    }  # fmt: skip
    for cle in ("expert", "optimise"):
        for jeu in ("dev", "validation"):
            v = list(out[cle][jeu].values())
            out[cle][jeu + "_moyenne"] = sum(v) / len(v)
        out[cle]["ecart_optimisme"] = out[cle]["dev_moyenne"] - out[cle]["validation_moyenne"]
    Path("autocal/resultats/etape3_optuna.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("essais", "params")}, indent=1))
    for cle in ("expert", "optimise"):
        print(cle, {k: round(v, 4) for k, v in out[cle].items() if k.endswith("moyenne") or k == "ecart_optimisme"})


if __name__ == "__main__":
    main()
