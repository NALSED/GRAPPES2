"""Calibration (Platt, régression isotone ; Brier, ECE, diagramme de fiabilité) et contrôles négatifs.

* Calibration (méthode 4) : le score devient une probabilité d'être juste (étiquette stricte : un non vérifiable est
  un « non juste »). Platt = ajustement sigmoïde (`LogisticRegression` sur le score) ; isotone =
  `IsotonicRegression`. Qualité : score de Brier, erreur de calibration espérée (ECE, intervalles de largeur égale),
  diagramme de fiabilité (`sklearn.calibration.calibration_curve`).
* Test de permutation (méthode 7) : on mélange les jugements entre les candidats d'un sujet et l'on vérifie que la
  R-précision s'effondre au niveau du hasard. Même mesure (`pytrec_eval`), graine fixée.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss

from grappes.autocal.metriques import evaluer


@dataclass(slots=True)
class Calibreur:
    methode: str  # "platt" | "isotone"
    modele: object

    def proba(self, scores) -> np.ndarray:
        s = np.asarray(scores, dtype=float)
        if self.methode == "platt":
            return self.modele.predict_proba(s.reshape(-1, 1))[:, 1]
        return self.modele.predict(s)


def ajuster(scores, etiquettes, methode: str) -> Calibreur:
    s, y = np.asarray(scores, dtype=float), np.asarray(etiquettes, dtype=int)
    if (
        len(set(y.tolist())) < 2
    ):  # une seule classe : aucune calibration possible, taux de base constant
        base = float(y.mean()) if len(y) else 0.0
        m = IsotonicRegression(out_of_bounds="clip").fit([0.0, 1.0], [base, base])
        return Calibreur("isotone", m)
    if methode == "platt":
        return Calibreur("platt", LogisticRegression(C=1e6, max_iter=1000).fit(s.reshape(-1, 1), y))
    return Calibreur(
        "isotone", IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(s, y)
    )


def ece(proba, etiquettes, bins: int = 10) -> float:
    """Erreur de calibration espérée : Σ_b (n_b/n)·|fréquence observée − confiance moyenne|."""
    p, y = np.asarray(proba, dtype=float), np.asarray(etiquettes, dtype=float)
    if len(p) == 0:
        return 0.0
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    return float(
        sum(
            abs(y[idx == b].mean() - p[idx == b].mean()) * (idx == b).sum() / len(p)
            for b in range(bins)
            if (idx == b).any()
        )
    )


def brier(proba, etiquettes) -> float:
    return (
        float(brier_score_loss(np.asarray(etiquettes, dtype=int), np.asarray(proba, dtype=float)))
        if len(proba)
        else 0.0
    )


def brier_base(etiquettes) -> float:
    """Brier du prédicteur constant (taux de base) : référence de comparaison."""
    y = np.asarray(etiquettes, dtype=float)
    return float(((y - y.mean()) ** 2).mean()) if len(y) else 0.0


def courbe(proba, etiquettes, bins: int = 10):
    """(fréquence observée, probabilité moyenne prédite) par intervalle non vide : points du diagramme de fiabilité."""
    y = np.asarray(etiquettes, dtype=int)
    if len(set(y.tolist())) < 2 and len(y):
        return np.array([]), np.array([])
    frac, moy = calibration_curve(
        y, np.asarray(proba, dtype=float), n_bins=bins, strategy="uniform"
    )
    return frac, moy


def diagramme(path: str, series: dict[str, tuple], titre: str = "") -> None:
    """Diagramme de fiabilité : une courbe par série {nom: (proba, étiquettes)} ; la diagonale = calibration parfaite."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], "k--", lw=1, label="calibration parfaite")
    for nom, (p, y) in series.items():
        frac, moy = courbe(p, y)
        if len(frac):
            ax.plot(
                moy, frac, marker="o", label=f"{nom} (ECE {ece(p, y):.3f}, Brier {brier(p, y):.3f})"
            )
    ax.set_xlabel("probabilité prédite")
    ax.set_ylabel("fréquence observée de « juste » (strict)")
    ax.set_title(titre)
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def permutation(
    sujet: str, docids: list[str], justes: set[str], r: int, n_perm: int = 1000, graine: int = 62
) -> dict[str, float]:
    """R-précision observée et distribution sous permutation des jugements entre les documents rendus.

    `docids` : documents rendus, du premier au dernier ; `justes` : ceux qui sont pertinents ; `r` : taille de la
    référence. Permuter = tirer au hasard quels documents rendus seraient pertinents (même nombre)."""
    k = sum(1 for d in docids if d in justes)
    run = {sujet: {d: float(len(docids) - i) for i, d in enumerate(docids)}}
    obs = (
        evaluer(
            {
                sujet: {d: 1 for d in docids if d in justes}
                | {f"__ref{i}": 1 for i in range(max(0, r - k))}
            },
            run,
        )[sujet]["Rprec"]
        if docids
        else 0.0
    )
    rng = random.Random(graine)
    vals = []
    for _ in range(n_perm):
        pick = set(rng.sample(docids, k)) if docids else set()
        qrels = {sujet: {d: 1 for d in pick} | {f"__ref{i}": 1 for i in range(max(0, r - k))}}
        vals.append(evaluer(qrels, run)[sujet]["Rprec"] if docids else 0.0)
    arr = np.asarray(vals)
    chance = (k / len(docids)) * min(r, len(docids)) / r if docids and r else 0.0
    return {
        "observe": float(obs),
        "permute_moyen": float(arr.mean()),
        "permute_p95": float(np.percentile(arr, 95)) if len(arr) else 0.0,
        "chance_attendu": float(chance),
        "p_valeur": float((arr >= obs).mean()) if len(arr) else 1.0,
        "k_justes": k,
        "n_rendus": len(docids),
    }
