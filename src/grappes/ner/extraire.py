"""Extraction d'organisations par des modèles existants (transformers), en multiprise, avec cache.

* Aucun mécanisme inventé : le modèle étiquette, ce module découpe le texte, applique le seuil de confiance et agrège.
* Garde-fou : une mention n'est gardée que si son segment (début, fin) existe dans le texte d'origine ; le nom rendu
  est ce segment-là, pas la sortie reformatée du modèle.
* Cache disque par (modèle, empreinte du texte) : un rejeu ne relance pas le modèle.
* Les doublons de variantes de nom (« Fenix Toulouse » / « Fenix Toulouse Handball ») sont SIGNALÉS par `variantes`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from grappes.canon import site_key
from grappes.semantic.scoring import normalize


@dataclass(frozen=True, slots=True)
class ModeleNer:
    id: str
    nom: str
    agregation: str = "simple"
    categories: dict[str, list[str]] = field(default_factory=dict)
    langues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NerConfig:
    modeles: tuple[ModeleNer, ...] = ()
    seuil_confiance: float = 0.85
    morceau_caracteres: int = 1200
    mots_max: int = 8
    longueur_min: int = 3
    min_pages: int = 1
    modeles_dir: str = "donnees/modeles"


def charger_config(dossier: str | Path = "config") -> NerConfig:
    d = Path(dossier)
    f = d / "ner.yaml"
    if not f.exists():
        return NerConfig()
    y = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    modeles = []
    for mid in y.get("actifs", []):
        m = yaml.safe_load((d / "ner" / f"{mid}.yaml").read_text(encoding="utf-8"))
        modeles.append(
            ModeleNer(
                mid, m["nom"], m.get("agregation", "simple"),
                {k: list(v) for k, v in (m.get("categories") or {}).items()}, tuple(m.get("langues", ())),
            )  # fmt: skip
        )
    return NerConfig(
        tuple(modeles), float(y.get("seuil_confiance", 0.85)), int(y.get("morceau_caracteres", 1200)),
        int(y.get("mots_max", 8)), int(y.get("mots_min_longueur", 3)), int(y.get("min_pages", 1)),
        str(y.get("modeles_dir", "donnees/modeles")),
    )  # fmt: skip


@dataclass(slots=True)
class Mention:
    debut: int
    fin: int
    score: float
    modele: str


def morceaux(texte: str, taille: int) -> list[tuple[int, str]]:
    """(décalage, morceau) : coupe à la fin d'une phrase quand c'est possible, sinon au dernier espace."""
    out, i = [], 0
    while i < len(texte):
        j = min(i + taille, len(texte))
        if j < len(texte):
            k = max(texte.rfind(". ", i, j), texte.rfind("\n", i, j))
            if k <= i + taille // 3:
                k = texte.rfind(" ", i, j)
            j = k + 1 if k > i else j
        if texte[i:j].strip():
            out.append((i, texte[i:j]))
        i = j
    return out


_PIPES: dict[tuple[str, str], object] = {}


def _pipeline(m: ModeleNer, modeles_dir: str):
    key = (m.nom, modeles_dir)
    if key not in _PIPES:
        import os

        os.environ.setdefault("HF_HOME", str(Path(modeles_dir).resolve()))
        from transformers import pipeline

        _PIPES[key] = pipeline("token-classification", model=m.nom, aggregation_strategy=m.agregation)
    return _PIPES[key]


def _recoller(ms: list[Mention], texte: str) -> list[Mention]:
    """Deux mentions séparées par une apostrophe ou un trait d'union (« OGC Nice Côte d' » + « Azur Handball »)
    sont un seul nom coupé par le découpage en sous-mots du modèle."""
    out: list[Mention] = []
    for m in sorted(ms, key=lambda x: x.debut):
        if out and texte[out[-1].fin : m.debut] in ("'", "’", "-") and out[-1].modele == m.modele:
            out[-1] = Mention(out[-1].debut, m.fin, min(out[-1].score, m.score), m.modele)
        else:
            out.append(m)
    return out


def _dict(x: Mention) -> dict:
    return {"debut": x.debut, "fin": x.fin, "score": x.score, "modele": x.modele}


def mentions_modele(texte: str, m: ModeleNer, cfg: NerConfig, cache: Path | None = None) -> list[Mention]:
    """Mentions d'organisations d'UN texte par UN modèle (cache disque). Toutes les mentions de catégorie retenue sont
    archivées avec leur score ; le seuil s'applique à la lecture (on peut donc le régler sans relancer le modèle)."""
    p = None
    if cache is not None:
        h = hashlib.sha256((m.nom + "\x00" + str(cfg.morceau_caracteres) + "\x00" + texte).encode()).hexdigest()[:24]
        p = cache / m.id / f"{h}.json"
        if p.exists():
            return [x for x in (Mention(**d) for d in json.loads(p.read_text(encoding="utf-8"))) if x.score >= cfg.seuil_confiance]
    pipe = _pipeline(m, cfg.modeles_dir)
    labels = {lab for ls in m.categories.values() for lab in ls}
    out: list[Mention] = []
    for dec, morceau in morceaux(texte, cfg.morceau_caracteres):
        for e in pipe(morceau):  # type: ignore[operator]
            if e.get("entity_group") not in labels:
                continue
            debut, fin = dec + int(e["start"]), dec + int(e["end"])
            if not (0 <= debut < fin <= len(texte)):
                continue  # garde-fou : le segment doit exister dans le texte d'origine
            out.append(Mention(debut, fin, float(e["score"]), m.id))
    out = _recoller(out, texte)
    if p is not None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps([_dict(x) for x in out]), encoding="utf-8")
    return [x for x in out if x.score >= cfg.seuil_confiance]


ARTICLE_TETE = re.compile(r"^(?:l'|l’|le |la |les |un |une |des |du |de la |de l'|d')", re.IGNORECASE)


def nettoyer(surface: str) -> str:
    s = re.sub(r"\s+", " ", surface).strip(" \t\n.,;:!?()[]«»\"'’-–—")
    return ARTICLE_TETE.sub("", s).strip()


def retenir(nom: str, cfg: NerConfig) -> bool:
    """Garde-fous sur la FORME du nom (pas sur le sens) : longueur, nombre de mots, au moins une majuscule."""
    mots = nom.split()
    return len(nom) >= cfg.longueur_min and 1 <= len(mots) <= cfg.mots_max and any(c.isupper() for c in nom) and not nom.isdigit()


@dataclass(slots=True)
class Nomme:
    cle: str  # nom normalisé
    nom: str  # forme de surface la plus fréquente
    surfaces: Counter = field(default_factory=Counter)
    pages: set[str] = field(default_factory=set)  # identifiants d'observation
    sites: set[str] = field(default_factory=set)  # clés de site des pages
    scores: list[float] = field(default_factory=list)
    modeles: set[str] = field(default_factory=set)
    score_par_site: dict[str, float] = field(default_factory=dict)  # éditeur -> meilleur score du modèle


def norm_cle(nom: str) -> str:
    return re.sub(r"[^\w]+", " ", normalize(nom)).strip()


def extraire(pages: list[tuple[str, str, str]], cfg: NerConfig, cache: Path | None = None) -> dict[str, Nomme]:
    """`pages` : (identifiant d'observation, clé de source, texte). Renvoie les organisations nommées, agrégées."""
    res: dict[str, Nomme] = {}
    for oid, src, texte in pages:
        site = site_key(src.partition(":")[2]) if src.startswith(("web:", "rss:")) else src
        for m in cfg.modeles:
            for me in mentions_modele(texte, m, cfg, cache):
                nom = nettoyer(texte[me.debut : me.fin])
                if not nom or not retenir(nom, cfg):
                    continue
                n = res.setdefault(norm_cle(nom), Nomme(norm_cle(nom), nom))
                n.surfaces[nom] += 1
                n.pages.add(oid)
                n.sites.add(site)
                n.score_par_site[site] = max(n.score_par_site.get(site, 0.0), me.score)
                n.scores.append(me.score)
                n.modeles.add(me.modele)
    for n in res.values():
        n.nom = n.surfaces.most_common(1)[0][0]
    return {k: v for k, v in res.items() if len(v.pages) >= cfg.min_pages}


def variantes(noms: dict[str, Nomme]) -> list[tuple[str, str]]:
    """Paires dont l'une est un préfixe de mots de l'autre (doublons de variantes de nom probables)."""
    cles = sorted(noms, key=len)
    return [(a, b) for i, a in enumerate(cles) for b in cles[i + 1 :] if b.startswith(a + " ") and len(a.split()) >= 2]
