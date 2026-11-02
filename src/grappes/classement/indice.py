"""Indice de fiabilité unique d'un candidat : rapport de vraisemblance (D64, étape 3), voir `grappes.vraisemblance`.

Éléments d'observation d'un candidat, chacun ramené à un log10 LR pondéré par la catégorie Admiralty de sa source :
registres (Wikidata, SIRENE/RNA), noms dans les textes (un éditeur = une voix), proximité du texte propre, citations
et écosystèmes. Les variantes d'un même nom sont regroupées par nomenklatura AVANT le calcul (une organisation, une
ligne, ses éléments réunis).
"""

from __future__ import annotations

import copy

from grappes import vraisemblance as vr
from grappes.classement.candidats import Candidat, Contexte
from grappes.variantes import regrouper


def elements(ctx: Contexte, c: Candidat, cfg: vr.LrConfig) -> list[vr.Element]:
    out: list[vr.Element] = []
    for r in sorted(c.registres):
        out.append(vr.Element(f"registre_{r}", r, "A", cfg.valeurs.get(f"registre_{r}", cfg.v("registre_source")) * vr.poids_lettre("A")))
    if c.ner_sites:
        parts = []
        for site, conf in sorted(c.ner_sites.items()):
            w = vr.poids_lettre(ctx.rel.letter(site))
            parts.append(vr.Element("nom_dans_texte", site, ctx.rel.letter(site), cfg.v("nom_dans_texte") * w * conf))
        cumul = min(sum(e.log10 for e in parts), cfg.v("nom_dans_texte_plafond"))
        k = sum(e.log10 for e in parts) or 1.0
        out += [vr.Element(e.nature, e.source, e.lettre, e.log10 * cumul / k) for e in parts]
    if c.propre:
        out.append(vr.Element("proximite_texte", c.ref, c.lettre, cfg.v("proximite_pente") * (c.s / 100.0 - cfg.v("proximite_pivot"))))
    if len(c.ecosystemes) > 1:
        out.append(vr.Element("ecosystemes", c.ref, c.lettre, cfg.v("ecosysteme") * (len(c.ecosystemes) - 1)))
    if c.editeurs:
        w = vr.poids_lettre(c.lettre)
        out.append(vr.Element("citations", c.ref, c.lettre, min(cfg.v("citation") * c.editeurs * w, cfg.v("citation_plafond"))))
    return out


def fusion_variantes(ctx: Contexte, cands: list[Candidat], seuil: float, lien: str = "simple") -> list[Candidat]:
    """Un candidat par acteur : les variantes de nom (nomenklatura) sont réunies sur le candidat le mieux documenté."""
    memo = ctx.memo_variantes
    noms = [c.nom for c in cands if c.nom]
    villes = [c.ville for c in cands if c.nom]
    cle = (tuple(noms), tuple(villes), seuil, lien)
    if cle not in memo:
        memo[cle] = regrouper(noms, seuil, villes=villes, lien=lien)
    avec_nom = [c for c in cands if c.nom]
    sans_nom = [c for c in cands if not c.nom]
    out: list[Candidat] = []
    for g in memo[cle]:
        membres = [avec_nom[i] for i in g]
        rep = copy.copy(max(membres, key=lambda c: (len(c.registres) + len(c.ner_sites) + len(c.citants), c.ref)))
        rep.registres, rep.ner_sites = set(rep.registres), dict(rep.ner_sites)
        rep.citants, rep.ecosystemes = set(rep.citants), set(rep.ecosystemes)
        for m in membres:
            if m.ref == rep.ref:
                continue
            rep.registres |= m.registres
            for s, v in m.ner_sites.items():
                rep.ner_sites[s] = max(rep.ner_sites.get(s, 0.0), v)
            rep.citants |= m.citants
            rep.ecosystemes |= m.ecosystemes
            rep.editeurs = max(rep.editeurs, m.editeurs)
            rep.ville = rep.ville or m.ville
            rep.propre = rep.propre or m.propre
            rep.s = max(rep.s, m.s)
        out.append(rep)
    return sorted(out + sans_nom, key=lambda c: c.ref)
