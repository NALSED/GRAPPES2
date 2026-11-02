"""Fusion des fournisseurs de candidats (D64) : registres (Wikidata, SIRENE/RNA) et noms lus dans les textes viennent
enrichir ou compléter les candidats du web.

Fusion : une entité de registre dont le site officiel est le domaine d'un candidat du web enrichit ce candidat (commune,
registre) au lieu d'en créer un second ; deux registres qui nomment le même acteur (même SIREN, sinon même nom
normalisé) ne comptent qu'une fois. Les variantes de nom restantes sont regroupées plus tard par nomenklatura
(`indice.fusion_variantes`) ; `variantes` ci-dessous ne fait que les SIGNALER.
"""

from __future__ import annotations

import copy
import re

from grappes.canon import canonical_domain, site_key
from grappes.classement.candidats import Candidat, Contexte
from grappes.decouverte.registres import Decouvert
from grappes.ner.extraire import Nomme
from grappes.semantic.scoring import normalize


def norm_name(name: str) -> str:
    """Casse, accents, ponctuation et espaces neutralisés (comparaison de libellés exacts)."""
    return re.sub(r"[^\w]+", " ", normalize(name)).strip()


def _groupes(ds: list[Decouvert]) -> list[dict]:
    """Un groupe par acteur : même SIREN, sinon même nom normalisé."""
    groupes: list[dict] = []
    par_siren: dict[str, dict] = {}
    par_nom: dict[str, dict] = {}
    for d in sorted(ds, key=lambda x: (x.registre != "wikidata", x.registre, x.identifiant)):
        g = (par_siren.get(d.siren) if d.siren else None) or par_nom.get(norm_name(d.nom))
        if g is None:
            g = {"nom": d.nom, "registres": set(), "ids": {}, "ville": "", "domaine": "", "siren": ""}
            groupes.append(g)
        g["registres"].add("sirene_naf" if d.registre == "sirene" and d.requete.startswith("NAF") else d.registre if d.registre != "sirene" else "sirene_nom")
        g["ids"].setdefault(d.registre, d.identifiant)
        g["ville"] = g["ville"] or d.ville
        g["domaine"] = g["domaine"] or d.domaine
        g["siren"] = g["siren"] or d.siren
        if d.siren:
            par_siren[d.siren] = g
        par_nom[norm_name(d.nom)] = g
    return groupes


def fusionner(ctx: Contexte, ds: list[Decouvert], sources: tuple[str, ...]) -> list[Candidat]:
    """Candidats du web (copies enrichies) + acteurs de registre sans équivalent web."""
    cands = [copy.copy(c) for c in ctx.candidats]
    par_site = {site_key(canonical_domain(c.ref.partition(":")[2])): c for c in cands if c.ref.startswith(("web:", "rss:"))}
    par_nom = {norm_name(c.nom): c for c in cands if c.nom}
    facteur = ctx.rel.table.factors["A"]
    for g in _groupes([d for d in ds if d.registre in sources]):
        site = site_key(canonical_domain(g["domaine"])) if g["domaine"] else ""
        c = par_site.get(site) if site else None
        c = c or par_nom.get(norm_name(g["nom"]))
        if c is not None:
            c.registres = c.registres | g["registres"]
            c.ville = c.ville or g["ville"]
            continue
        reg = "wikidata" if "wikidata" in g["registres"] else "sirene" if "sirene" in g["ids"] else sorted(g["ids"])[0]
        ref = f"reg:{reg}:{g['ids'][reg]}" + (f"|{g['siren']}" if g["siren"] else "")
        cands.append(
            Candidat(ref, g["nom"], g["nom"], 0.0, "registre", 0.0, 0.0, 0.0, facteur=facteur,
                     registres=set(g["registres"]), ville=g["ville"])
        )  # fmt: skip
    return sorted(cands, key=lambda c: c.ref)


def variantes(cands: list[Candidat]) -> list[tuple[str, str]]:
    """Paires de noms dont l'un est un préfixe de mots de l'autre (doublons probables), à signaler."""
    noms = sorted({norm_name(c.nom) for c in cands if c.nom and c.registres}, key=len)
    out = []
    for i, a in enumerate(noms):
        for b in noms[i + 1 :]:
            if b != a and b.startswith(a + " ") and len(a.split()) >= 2:
                out.append((a, b))
    return out


def ajouter(
    ctx: Contexte, cands: list[Candidat], nommes: dict[str, Nomme]
) -> tuple[list[Candidat], list[tuple[str, str]]]:
    """(candidats avec les entités nommées, citations source -> entité nommée)."""
    cands = list(cands)
    par_nom = {norm_name(c.nom): c for c in cands if c.nom}
    cits: list[tuple[str, str]] = []
    obs_source = {o.obs_id: o.source.key for o in ctx.db.load_observations()}
    for cle, n in sorted(nommes.items()):
        c = par_nom.get(norm_name(n.nom))
        if c is None:
            c = Candidat(
                f"ner:{cle}", n.nom, n.nom, 0.0, "nomme", 0.0, 0.0, 0.0,
                facteur=ctx.rel.factor(f"ner:{cle}"), citants=set(), ecosystemes=set(),
            )  # fmt: skip
            cands.append(c)
            par_nom[norm_name(n.nom)] = c
        for site, conf in n.score_par_site.items():
            c.ner_sites[site] = max(c.ner_sites.get(site, 0.0), conf)
        for oid in sorted(n.pages):
            if oid in obs_source:
                cits.append((obs_source[oid], c.ref))
    return sorted(cands, key=lambda c: c.ref), sorted(set(cits))
