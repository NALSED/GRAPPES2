"""Indice de fiabilité unique : rapport de vraisemblance (cadre ENFSI), voir `config/lr.yaml`.

Un élément = (nature, source, lettre Admiralty, log10 LR). La nature de la source disparaît du calcul : il ne reste
que son poids. Le total est la somme bornée des log10 ; la probabilité a posteriori n'existe qu'APRÈS calibration
(`autocal.controles`), pas ici : le LR seul ne dit pas combien de chances sur cent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from grappes.fiabilite import LETTERS, letter_factors


@dataclass(frozen=True, slots=True)
class Element:
    nature: str
    source: str
    lettre: str
    log10: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LrConfig:
    borne_element: float = 3.0
    borne_totale: float = 6.0
    valeurs: dict[str, float] = field(default_factory=dict)
    bandes: tuple[tuple[float, str], ...] = ()

    def v(self, nom: str) -> float:
        return self.valeurs[nom]

    def avec(self, **kw: float) -> LrConfig:
        return replace(self, valeurs={**self.valeurs, **kw})


def charger(path: str | Path = "config/lr.yaml") -> LrConfig:
    y = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return LrConfig(
        float(y["bornes"]["par_element"]),
        float(y["bornes"]["total"]),
        {k: float(v) for k, v in y["elements"].items()},
        tuple((float(b["borne"]), str(b["libelle"])) for b in y["bandes"]),
    )


def poids_lettre(lettre: str) -> float:
    """Poids d'une source selon sa catégorie Admiralty : facteur de la lettre / facteur de A (A = 1)."""
    f = letter_factors()
    return f[lettre if lettre in LETTERS else "F"] / f["A"]


def borne(x: float, b: float) -> float:
    return max(-b, min(b, x))


def total(elements: list[Element], cfg: LrConfig) -> float:
    """log10 LR total : somme des éléments bornés individuellement, puis bornée."""
    return borne(sum(borne(e.log10, cfg.borne_element) for e in elements), cfg.borne_totale)


def bande(log10_lr: float, cfg: LrConfig) -> str:
    """Restitution verbale. LR < 1 : mêmes bandes, en faveur de H2."""
    lr = 10 ** abs(log10_lr)
    for borne_, lib in cfg.bandes:
        if lr <= borne_ or math.isinf(borne_):
            return lib if log10_lr >= 0 else lib.replace("soutien", "soutien contre", 1) if lib != "aucun apport" else lib
    return cfg.bandes[-1][1]  # pragma: no cover
