"""Métriques de classement : recherche d'information classique, calculées par `pytrec_eval` (binding de trec_eval,
NIST) depuis un fichier de jugements (qrels) et un fichier de résultats (run). Aucune métrique maison
(DECISION_AUTOCAL.md, méthode 3).

* R-précision (`Rprec`) : précision aux R premiers, R = nombre d'entités attendues du sujet. Les documents non
  jugés comptent comme non pertinents : un non vérifiable n'est JAMAIS compté juste (borne basse stricte).
* `bpref` (Buckley & Voorhees 2004) et `infAP` (inferred AP, NIST) : conçues pour des jugements incomplets.
* P@k, nDCG, MAP : compléments.
"""

from __future__ import annotations

from pathlib import Path

import pytrec_eval

MESURES = {"Rprec", "bpref", "infAP", "map", "ndcg", "P_5", "P_10", "P_20", "P_50", "recall_100"}


def lire_qrels(path: str | Path) -> dict[str, dict[str, int]]:
    """Format qrels TREC : `sujet 0 docid pertinence`."""
    out: dict[str, dict[str, int]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            q, _, d, r = line.split()
            out.setdefault(q, {})[d] = int(r)
    return out


def ecrire_run(path: str | Path, run: dict[str, dict[str, float]], tag: str = "grappes") -> None:
    """Format run TREC : `sujet Q0 docid rang score tag`."""
    lines = []
    for q, docs in sorted(run.items()):
        for rank, (d, s) in enumerate(sorted(docs.items(), key=lambda kv: (-kv[1], kv[0])), 1):
            lines.append(f"{q} Q0 {d} {rank} {s:.6f} {tag}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def evaluer(
    qrels: dict[str, dict[str, int]], run: dict[str, dict[str, float]]
) -> dict[str, dict[str, float]]:
    """Mesures par sujet. Un sujet sans résultat reçoit zéro partout (jamais ignoré). Un sujet dont la référence
    est vide (contrôle négatif) n'a pas de R-précision définie : voir `controle_negatif`."""
    ev = pytrec_eval.RelevanceEvaluator(qrels, MESURES)
    scored = ev.evaluate({q: (run.get(q) or {"__aucun__": 0.0}) for q in qrels})
    return {q: dict(m) for q, m in scored.items()}


def moyenne(
    par_sujet: dict[str, dict[str, float]], mesure: str, sujets: list[str] | None = None
) -> float:
    vals = [m[mesure] for q, m in par_sujet.items() if sujets is None or q in sujets]
    return sum(vals) / len(vals) if vals else 0.0
