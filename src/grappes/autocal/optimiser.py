"""Optimisation d'hyperparamètres : Optuna (TPE), étude stockée en SQLite, reprise après coupure, arrêt anticipé
avec patience (DECISION_AUTOCAL.md, méthode 5). Pas de grille : recherche aléatoire puis bayésienne (Bergstra &
Bengio 2012 ; TPE). Chaque essai est archivé avec ses paramètres et son score : c'est le registre de preuve.

Ce module ne règle que le CLASSEMENT (méthode de découverte de vérité, poids, variantes) sur des données archivées :
zéro requête réseau. La validation imbriquée et le jeu de test réservé sont dans `protocole.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import optuna

from grappes.autocal import evaluation as ev
from grappes.autocal.jeux import DonneesSujet

ALGOS = ("vote", "sums", "averagelog", "investment", "pooledinvestment", "truthfinder")
STORAGE = "sqlite:///donnees/autocal/optuna.db"


def params_depuis(trial: optuna.Trial) -> ev.Params:
    """Espace de recherche (bornes fixées avant les résultats)."""
    methode = trial.suggest_categorical("methode", ALGOS)
    w_s = trial.suggest_float("w_s", 0.0, 1.0)
    w_t = trial.suggest_float("w_t", 0.0, 1.0)
    w_f = trial.suggest_float("w_f", 0.0, 1.0)
    if w_s + w_t + w_f < 1e-6:
        w_s = 1.0
    return ev.Params(
        methode=methode,
        w_s=w_s,
        w_t=w_t,
        w_f=w_f,
        negatifs=trial.suggest_categorical("negatifs", [False, True]),
        g=trial.suggest_float("g", 1.0, 2.0),
        rho=trial.suggest_float("rho", 0.0, 1.0),
        gamma=trial.suggest_float("gamma", 0.05, 0.95),
        editeur_unique=trial.suggest_categorical("editeur_unique", [True, False]),
        nom_requis=trial.suggest_categorical("nom_requis", [True, False]),
    )


def score_jeu(
    donnees: list[DonneesSujet], p: ev.Params, mesure: str = "Rprec"
) -> tuple[float, dict[str, dict]]:
    """Moyenne (sujets positifs) de la mesure choisie, R-précision stricte par défaut ; détail par sujet."""
    detail: dict[str, dict] = {}
    vals = []
    for d in donnees:
        if d.ref is None or d.juge is None:
            continue
        jug = ev.juger_classement(d.juge, ev.classer(d.ctx, p))
        r = ev.mesurer(d.ref, jug)
        detail[d.id] = r
        vals.append(r["mesures"][mesure])
    return (sum(vals) / len(vals) if vals else 0.0), detail


class Patience:
    """Arrêt anticipé : l'étude s'arrête quand le meilleur score n'a pas progressé de `delta` en `n` essais."""

    def __init__(self, n: int = 150, delta: float = 1e-4):
        self.n, self.delta = n, delta

    def __call__(self, study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        done = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
        if len(done) < self.n:
            return
        best_i = max(range(len(done)), key=lambda i: (done[i].value, -i))
        first_best = min(
            i for i, t in enumerate(done) if t.value >= done[best_i].value - self.delta
        )
        if len(done) - 1 - first_best >= self.n:
            study.stop()


def etude(
    nom: str,
    objectif: Callable[[optuna.Trial], float],
    essais: int,
    graine: int = 62,
    patience: int = 150,
    storage: str = STORAGE,
) -> optuna.Study:
    """Crée ou reprend l'étude `nom` (SQLite) et lance jusqu'à `essais` essais en tout."""
    Path("donnees/autocal").mkdir(parents=True, exist_ok=True)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    st = optuna.create_study(
        study_name=nom,
        storage=storage,
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=graine, n_startup_trials=40),
        load_if_exists=True,
    )
    restant = max(0, essais - len(st.trials))
    if restant:
        st.optimize(objectif, n_trials=restant, callbacks=[Patience(patience)])
    return st
