"""Classement de production (D64, étape 3) : candidats du web + registres + noms lus -> indice de vraisemblance ->
probabilité calibrée -> lignes de sortie. Aucune dépendance à la mission d'évaluation (`autocal`)."""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from grappes import avancement
from grappes import vraisemblance as vr
from grappes.classement import fusion, indice
from grappes.classement.candidats import Candidat, Contexte, charger
from grappes.config import Config
from grappes.decouverte.client import Http
from grappes.decouverte.registres import DecouverteConfig, decouvrir
from grappes.ner.extraire import charger_config as charger_ner
from grappes.ner.extraire import extraire
from grappes.sortie import LigneSortie
from grappes.storage.db import Database


@dataclass(frozen=True, slots=True)
class ClassementConfig:
    actif: bool = False
    remplace_resultat: bool = False
    registres: tuple[str, ...] = ("wikidata", "sirene")
    noms: bool = True
    variantes: bool = False
    seuil_variantes: float = 0.9
    n_max: int = 200
    p_min: float = 0.0
    calibration: str = "config/calibration.json"
    cache_jours: int = 30
    lien_variantes: str = "complet"  # en dernier : la configuration est construite par position


def charger_config(config_dir: str | Path) -> ClassementConfig:
    f = Path(config_dir) / "classement.yaml"
    if not f.exists():
        return ClassementConfig()
    y = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    fo, va, so = y.get("fournisseurs") or {}, y.get("variantes") or {}, y.get("sortie") or {}
    return ClassementConfig(
        bool(y.get("actif", False)), bool(y.get("remplace_resultat", False)),
        tuple(fo.get("registres", ("wikidata", "sirene"))), bool(fo.get("noms", True)),
        bool(va.get("actif", False)), float(va.get("seuil", 0.9)),
        int(so.get("n_max", 200)), float(so.get("p_min", 0.0)),
        str(y.get("calibration", "config/calibration.json")), int(y.get("cache_registres_jours", 30)),
        str(va.get("lien", "complet")),
    )  # fmt: skip


@dataclass(slots=True)
class Calibreur:
    """Transformation log10 LR -> probabilité, lue d'un fichier (`config/calibration.json`) : sigmoïde (Platt) ou
    régression isotone (points de rupture). Sans fichier : sigmoïde de rapport de cotes 1:1, NON calibrée."""

    methode: str = "aucune"
    a: float = math.log(10)
    b: float = 0.0
    x: list[float] = field(default_factory=list)
    y: list[float] = field(default_factory=list)
    valide: bool = False  # vrai seulement si la calibration a prouvé un pouvoir discriminant (Brier < constant)
    note: str = "non calibré"

    def proba(self, log10_lr: float) -> float:
        if self.methode == "isotone" and self.x:
            i = bisect.bisect_left(self.x, log10_lr)
            if i <= 0:
                return self.y[0]
            if i >= len(self.x):
                return self.y[-1]
            x0, x1, y0, y1 = self.x[i - 1], self.x[i], self.y[i - 1], self.y[i]
            return y0 if x1 == x0 else y0 + (y1 - y0) * (log10_lr - x0) / (x1 - x0)
        return 1.0 / (1.0 + math.exp(-(self.a * log10_lr + self.b)))


def charger_calibreur(path: str | Path) -> Calibreur:
    f = Path(path)
    if not f.exists():
        return Calibreur()
    d = json.loads(f.read_text(encoding="utf-8"))
    return Calibreur(d["methode"], d.get("a", 1.0), d.get("b", 0.0), d.get("x", []), d.get("y", []), bool(d.get("valide", False)), d.get("note", ""))


def classer(ctx: Contexte, cfg: ClassementConfig, lr: vr.LrConfig, cal: Calibreur) -> list[tuple[Candidat, float, float]]:
    """(candidat, log10 LR, probabilité calibrée), du mieux au moins bien classé ; ex æquo : ordre alphabétique."""
    sources = set(cfg.registres)
    if "yaml" in sources:  # prises décrites en YAML : leurs entités portent l'identifiant de la prise (D68)
        sources |= {d.registre for d in ctx.decouverts}
    cands = fusion.fusionner(ctx, ctx.decouverts, tuple(sources)) if cfg.registres else list(ctx.candidats)
    if cfg.noms:
        cands, _ = fusion.ajouter(ctx, cands, ctx.nommes)
    cands = [c for c in cands if c.nom]
    if cfg.variantes:
        cands = indice.fusion_variantes(ctx, cands, cfg.seuil_variantes, cfg.lien_variantes)
    out = []
    for c in cands:
        lg = vr.total(indice.elements(ctx, c, lr), lr)
        out.append((c, lg, cal.proba(lg)))
    return sorted(out, key=lambda t: (-t[1], t[0].ref))


def lignes(classes: list[tuple[Candidat, float, float]], cfg: ClassementConfig, lr: vr.LrConfig, cal: Calibreur, pays: str) -> list[LigneSortie]:
    rendu = [t for t in classes if t[2] >= cfg.p_min][: cfg.n_max]
    calibre = "mesuré" if cal.valide else "estimation étendue"
    return [
        LigneSortie(
            c.nom or c.nom_brut, c.ville, pays, min(100.0 * p, 99.0), vr.bande(lg, lr), calibre,
            sorted({c.ref, *c.ner_sites}), sorted(c.registres), "retenu",
        )
        for c, lg, p in rendu
    ]  # fmt: skip


def classer_production(db: Database, cfg: Config, config_dir: str | Path, data_dir: str | Path, pays: str = "FR", http: Http | None = None) -> list[LigneSortie]:
    """Chaîne complète pour le premier sujet de la configuration. Les registres sont interrogés (cache disque à durée
    de vie limitée) ; les noms sont extraits des textes déjà en base (cache disque par texte)."""
    cc = charger_config(config_dir)
    ctx = charger(None, cfg, db)
    phrase = cfg.grappes[0].subject
    data = Path(data_dir)
    if cc.registres:
        http = http or Http(data / "decouverte_cache", delai_s=1.0, ttl_jours=cc.cache_jours)
        ctx.decouverts = decouvrir(phrase, http, DecouverteConfig(sources=tuple(cc.registres), naf=str(Path(config_dir) / "naf_rev2.csv"), dossier_sources=str(Path(config_dir) / "recherche")))
    if cc.noms:
        ner = charger_ner(config_dir)
        ctx.nommes = extraire([(o.obs_id, o.source.key, o.text) for o in db.load_observations()], ner, data / "ner_cache") if ner.modeles else {}
    lr = vr.charger(Path(config_dir) / "lr.yaml")
    cal = charger_calibreur(Path(config_dir) / Path(cc.calibration).name)
    return lignes(classer(ctx, cc, lr, cal), cc, lr, cal, pays)


def ambiguites(cfg: Config, data_dir: str | Path, http: Http | None = None) -> list[dict]:
    """Lectures possibles du sujet quand il y en a plusieurs (D68, chantier B). Liste seulement : aucun départage."""
    from dataclasses import asdict

    from grappes.decouverte.traduction import lectures

    http = http or Http(Path(data_dir) / "decouverte_cache", delai_s=1.0, ttl_jours=30)
    lec = lectures(cfg.grappes[0].subject, http)
    return [asdict(x) for x in lec] if len(lec) > 1 else []


def appliquer(db: Database, cfg: Config, config_dir: str | Path, out_dir: str | Path, data_dir: str | Path, pays: str = "FR") -> list[Path]:
    """Écrit les profils de sortie issus du classement si `config/classement.yaml` l'active (sinon : rien).
    Une erreur (réseau, modèle absent) n'interrompt pas le cycle : elle est écrite dans `classement_erreur.txt`."""
    from grappes.sortie import charger_profils
    from grappes.sortie import ecrire as ecrire_profils

    cc = charger_config(config_dir)
    out = Path(out_dir)
    if not cc.actif:
        return []
    erreur = out / "classement_erreur.txt"
    try:
        avancement.signaler("etape", etape="verification", detail="classement")
        ls = classer_production(db, cfg, config_dir, data_dir, pays)
        avancement.signaler("noms", n=len(ls))
        avancement.signaler("etape", etape="sortie", detail="profils")
        ignorer = frozenset() if cc.remplace_resultat else frozenset({"resultat", "tsites"})
        chemins = ecrire_profils(ls, charger_profils(config_dir), out, ignorer=ignorer)
        erreur.unlink(missing_ok=True)
        amb = out / "ambiguite.json"
        lec = ambiguites(cfg, data_dir)
        if lec:
            amb.write_text(json.dumps({
                "sujet": cfg.grappes[0].subject, "ambigu": True, "lectures": lec,
                "note": "Plusieurs lectures du sujet existent ; aucune n'est choisie par une règle. Les sources en plein texte (data.gouv.fr, noms lus) les couvrent toutes sans les distinguer ; la classe Wikidata retenue, si elle est utilisée, est la première trouvée.",
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        else:
            amb.unlink(missing_ok=True)
        return chemins
    except Exception as exc:  # noqa: BLE001 - le classement est un ajout : il ne doit jamais faire échouer le cycle
        out.mkdir(parents=True, exist_ok=True)
        erreur.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        return []
