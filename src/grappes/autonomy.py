"""Configuration d'autonomie : `config/autonomie.yaml` (limites, interdits, interrupteur) et
`config/sujets.yaml` (sujets en une phrase). C'est tout ce que Sednal fournit (FINAL.md § 2).

Règles générales de collecte (codées, non configurables à la baisse) : sources publiques
uniquement, robots.txt respecté, ni connexion ni compte, ni contournement de limite ou de captcha,
délai d'au moins 1 s par domaine, arrêt sur 403/429, User-Agent identifiable. Sednal édite seulement
la liste des INTERDITS (domaines, motifs) et les quotas.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from grappes.config import ConfigError

MIN_DOMAIN_DELAY_S = 1.0  # plancher imposé par les règles générales
DEFAULT_UA = "grappes-collecte/1 (collecte automatique de pages publiques ; respecte robots.txt)"
# Réseaux sociaux : jamais lus par le récupérateur de pages (leurs liens deviennent des citations).
DEFAULT_SOCIAL = (
    "x.com", "twitter.com", "t.co", "t.me", "telegram.me", "instagram.com", "facebook.com",
    "fb.com", "tiktok.com", "linkedin.com", "snapchat.com", "threads.net", "whatsapp.com",
    "bsky.app", "youtube.com", "youtu.be", "pinterest.com", "tumblr.com", "vk.com", "discord.com",
    "twitch.tv", "reddit.com", "mastodon.social", "wa.me",
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Autonomy:
    autonome: bool = False  # interrupteur : false = dry-run, rien n'est écrit ni appelé
    stop_file: Path = Path("donnees/STOP")  # sa présence arrête tout immédiatement
    # limites générales
    requetes_par_cycle: int = 50
    pages_par_cycle: int = 100
    nouvelles_sources_par_cycle: int = 20
    elements_par_cycle: int = 200  # observations ingérées au plus par cycle
    profondeur: int = 1  # 0 = résultats de recherche seulement ; 1 = + liens sortants des pages
    seuil_ingestion: float = 20.0  # proximité minimale (0 à 100) avec un sujet (valeur de départ)
    seuil_ingestion_min: float = 5.0  # bornes de l'ajustement automatique
    seuil_ingestion_max: float = 40.0
    seuil_ingestion_auto: bool = False  # vrai : la boucle l'ajuste elle-même dans les bornes
    ingestion_termes_min: int = (
        2  # radicaux DIFFÉRENTS du lexique exigés (un mot répété ne fait pas un sujet)
    )
    # interdits (éditables)
    interdits_domaines: tuple[str, ...] = ()
    interdits_motifs: tuple[str, ...] = ()
    # collecte
    delai_domaine_s: float = MIN_DOMAIN_DELAY_S
    user_agent: str = DEFAULT_UA
    reseaux_non_lus: tuple[str, ...] = DEFAULT_SOCIAL
    max_octets_page: int = 1_000_000
    # amorçage
    amorcage_max_termes: int = 20
    amorcage_poids: float = 0.4
    requetes_termes: int = 4  # termes du lexique combinés par deux avec la phrase (4 = 2 requêtes en plus de la phrase)
    # sortie de compatibilité resultat_tsites.csv (D59)
    export_tsites_actif: bool = True
    export_tsites_pays_defaut: str = "FR"
    # apprentissage
    apprentissage_actif: bool = True
    min_confirmees: int = 3
    min_ecosystemes: int = 2
    max_termes_par_cycle: int = 5
    max_termes_total: int = 30
    poids_appris: float = 0.3
    expiration_cycles: int = 4
    support_min: float = 0.3  # part minimale des entités confirmées qui contiennent le terme
    rarete_max: float = 0.1  # part maximale des autres entités qui le contiennent
    exiger_racine_de_confiance: bool = (
        True  # un enseignant doit descendre d'une racine de confiance
    )
    # recalibrage borné
    recalibrage_actif: bool = True
    variation_max: float = 5.0
    bornes_seuil_min: tuple[float, float] = (30.0, 60.0)
    bornes_marge: tuple[float, float] = (5.0, 20.0)
    # stabilité : au-delà, retour en arrière automatique
    max_changement_grappe: float = 0.25
    max_taux_ambiguite: float = 0.5
    chute_max_confirmes: float = 0.3
    max_derive_lexique: float = 1.0
    min_entites: int = 10  # en dessous, trop peu d'entités rangées pour juger de la stabilité
    instantanes_conserves: int = 10
    rapports_dir: Path = Path("rapports")


@dataclass(frozen=True, slots=True)
class Subject:
    id: str  # identifiant de grappe dérivé de la phrase
    phrase: str


@dataclass(frozen=True, slots=True)
class Subjects:
    subjects: tuple[Subject, ...] = field(default_factory=tuple)


def _yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: un objet YAML est attendu")
    return data


def _num(d: dict[str, Any], key: str, default: float, path: Path, minimum: float = 0.0) -> float:
    try:
        v = float(d.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: {key} doit être un nombre") from exc
    if not math.isfinite(v) or v < minimum:
        raise ConfigError(f"{path}: {key} doit être un nombre fini >= {minimum:g}")
    return v


def _strs(d: dict[str, Any], key: str, default: tuple[str, ...], path: Path) -> tuple[str, ...]:
    raw = d.get(key)
    if raw is None:
        return default
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise ConfigError(f"{path}: {key} doit être une liste de textes non vides")
    return tuple(dict.fromkeys(x.strip().casefold() for x in raw))


def _bounds(d: dict[str, Any], key: str, default: tuple[float, float], path: Path):
    raw = d.get(key)
    if raw is None:
        return default
    if not (isinstance(raw, list) and len(raw) == 2):
        raise ConfigError(f"{path}: {key} doit être [min, max]")
    lo, hi = (_num({"v": x}, "v", 0, path) for x in raw)
    if not 0 <= lo <= hi <= 100:
        raise ConfigError(f"{path}: {key} doit vérifier 0 <= min <= max <= 100")
    return (lo, hi)


_QUOTA_KEYS = (
    "requetes_par_cycle",
    "pages_par_cycle",
    "nouvelles_sources_par_cycle",
    "elements_par_cycle",
)


def _normalize_autonomy(d: dict[str, Any], path: Path) -> dict[str, Any]:
    """Accepte aussi la forme courte : quotas groupés sous `quotas:`, `profondeur_max` pour
    `profondeur`, et `interdits` en simple liste (un nom de domaine -> domaine interdit, sinon motif).
    Le fichier de Sednal n'est jamais réécrit."""
    d = dict(d)
    quotas = d.pop("quotas", None)
    if quotas is not None:
        if not isinstance(quotas, dict):
            raise ConfigError(f"{path}: quotas doit être un mapping")
        for k, v in quotas.items():
            d.setdefault(k, v)
    if "profondeur_max" in d:
        d.setdefault("profondeur", d.pop("profondeur_max"))
    interdits = d.get("interdits")
    if isinstance(interdits, list):
        if not all(isinstance(x, str) and x.strip() for x in interdits):
            raise ConfigError(f"{path}: interdits doit être une liste de textes non vides")
        domaines = [x for x in interdits if re.fullmatch(r"[\w-]+(\.[\w-]+)+", x.strip())]
        d["interdits"] = {
            "domaines": domaines,
            "motifs": [x for x in interdits if x not in domaines],
        }
    return d


def _ingestion_settings(
    d: dict[str, Any], path: Path, config_dir
) -> tuple[float, float, float, bool]:
    """`seuil_ingestion` : un nombre, ou `{valeur, min, max, auto_ajustable}`. Si l'ajustement automatique est
    permis, la dernière valeur ajustée par la boucle (fichier appris) remplace la valeur de départ, toujours
    ramenée dans les bornes."""
    from grappes.learned import LearnedError, load_ingestion_threshold

    base = Autonomy()  # (attributs de classe d'un dataclass à slots : il faut une instance)
    raw = d.get("seuil_ingestion", base.seuil_ingestion)
    if isinstance(raw, dict):
        start = _num(raw, "valeur", base.seuil_ingestion, path)
        lo = _num(raw, "min", base.seuil_ingestion_min, path)
        hi = _num(raw, "max", base.seuil_ingestion_max, path)
        auto = bool(raw.get("auto_ajustable", False))
    else:
        start = _num(d, "seuil_ingestion", base.seuil_ingestion, path)
        lo, hi, auto = base.seuil_ingestion_min, base.seuil_ingestion_max, False
    if not 0 <= lo <= hi <= 100:
        raise ConfigError(f"{path}: seuil_ingestion : 0 <= min <= max <= 100 attendu")
    value = min(max(start, lo), hi)
    if auto:
        try:
            learned = load_ingestion_threshold(config_dir)
        except LearnedError as exc:
            raise ConfigError(str(exc)) from exc
        if learned is not None:
            value = min(max(learned, lo), hi)
    return value, lo, hi, auto


def _export_tsites(section: Any, path: Path, base: Autonomy) -> tuple[bool, str]:
    """Section `export_tsites` : `actif` (booléen, vrai par défaut) et `pays_defaut` (code ISO 2 lettres, FR)."""
    if section is None:
        return base.export_tsites_actif, base.export_tsites_pays_defaut
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: export_tsites doit être un mapping (actif, pays_defaut)")
    actif = section.get("actif", base.export_tsites_actif)
    if not isinstance(actif, bool):
        raise ConfigError(f"{path}: export_tsites.actif doit être true ou false")
    pays = section.get("pays_defaut", base.export_tsites_pays_defaut)
    if not isinstance(pays, str) or pays.strip() and not re.fullmatch(r"[A-Za-z]{2}", pays.strip()):
        raise ConfigError(f"{path}: export_tsites.pays_defaut doit être un code pays de 2 lettres")
    return actif, pays.strip().upper()


def load_autonomy(config_dir: str | Path = "config") -> Autonomy:
    """`config/autonomie.yaml` ; absent = tout par défaut, donc `autonome: false`."""
    path = Path(config_dir) / "autonomie.yaml"
    if not path.exists():
        return Autonomy()
    d = _normalize_autonomy(_yaml(path), path)
    ingestion = _ingestion_settings(d, path, config_dir)
    base = Autonomy()
    ap, rc, st = (d.get(k) or {} for k in ("apprentissage", "recalibrage", "stabilite"))
    for name, section in (("apprentissage", ap), ("recalibrage", rc), ("stabilite", st)):
        if not isinstance(section, dict):
            raise ConfigError(f"{path}: {name} doit être un mapping")
    amorcage = d.get("amorcage") or {}
    if not isinstance(amorcage, dict):
        raise ConfigError(f"{path}: amorcage doit être un mapping")
    interdits = d.get("interdits") or {}
    if not isinstance(interdits, dict):
        raise ConfigError(f"{path}: interdits doit être un mapping (domaines, motifs)")
    tsites = _export_tsites(d.get("export_tsites"), path, base)
    ua = d.get("user_agent", base.user_agent)
    if not isinstance(ua, str) or not ua.strip():
        raise ConfigError(f"{path}: user_agent doit être un texte (identifiable)")
    return Autonomy(
        autonome=bool(d.get("autonome", False)),
        stop_file=Path(str(d.get("stop_file", base.stop_file))),
        requetes_par_cycle=int(_num(d, "requetes_par_cycle", base.requetes_par_cycle, path)),
        pages_par_cycle=int(_num(d, "pages_par_cycle", base.pages_par_cycle, path)),
        nouvelles_sources_par_cycle=int(
            _num(d, "nouvelles_sources_par_cycle", base.nouvelles_sources_par_cycle, path)
        ),
        elements_par_cycle=int(_num(d, "elements_par_cycle", base.elements_par_cycle, path)),
        profondeur=int(_num(d, "profondeur", base.profondeur, path)),
        seuil_ingestion=ingestion[0],
        seuil_ingestion_min=ingestion[1],
        seuil_ingestion_max=ingestion[2],
        seuil_ingestion_auto=ingestion[3],
        ingestion_termes_min=int(
            _num(d, "ingestion_termes_min", base.ingestion_termes_min, path, 1)
        ),
        interdits_domaines=_strs(interdits, "domaines", (), path),
        interdits_motifs=_strs(interdits, "motifs", (), path),
        delai_domaine_s=max(
            MIN_DOMAIN_DELAY_S, _num(d, "delai_domaine_s", MIN_DOMAIN_DELAY_S, path)
        ),
        user_agent=ua.strip(),
        reseaux_non_lus=tuple(
            dict.fromkeys((*DEFAULT_SOCIAL, *_strs(d, "reseaux_non_lus", (), path)))
        ),
        max_octets_page=int(_num(d, "max_octets_page", base.max_octets_page, path, 1000)),
        amorcage_max_termes=int(_num(amorcage, "max_termes", base.amorcage_max_termes, path)),
        amorcage_poids=_num(amorcage, "poids", base.amorcage_poids, path),
        requetes_termes=int(_num(d, "requetes_termes", base.requetes_termes, path, 2)),
        export_tsites_actif=tsites[0],
        export_tsites_pays_defaut=tsites[1],
        apprentissage_actif=bool(ap.get("actif", True)),
        min_confirmees=int(_num(ap, "min_confirmees", base.min_confirmees, path, 1)),
        min_ecosystemes=int(_num(ap, "min_ecosystemes", base.min_ecosystemes, path, 1)),
        max_termes_par_cycle=int(_num(ap, "max_termes_par_cycle", base.max_termes_par_cycle, path)),
        max_termes_total=int(_num(ap, "max_termes_total", base.max_termes_total, path)),
        poids_appris=_num(ap, "poids", base.poids_appris, path),
        expiration_cycles=int(_num(ap, "expiration_cycles", base.expiration_cycles, path, 1)),
        support_min=_num(ap, "support_min", base.support_min, path),
        rarete_max=_num(ap, "rarete_max", base.rarete_max, path),
        exiger_racine_de_confiance=bool(ap.get("exiger_racine_de_confiance", True)),
        recalibrage_actif=bool(rc.get("actif", True)),
        variation_max=_num(rc, "variation_max", base.variation_max, path),
        bornes_seuil_min=_bounds(rc, "bornes_seuil_min", base.bornes_seuil_min, path),
        bornes_marge=_bounds(rc, "bornes_marge", base.bornes_marge, path),
        max_changement_grappe=_num(st, "max_changement_grappe", base.max_changement_grappe, path),
        max_taux_ambiguite=_num(st, "max_taux_ambiguite", base.max_taux_ambiguite, path),
        chute_max_confirmes=_num(st, "chute_max_confirmes", base.chute_max_confirmes, path),
        max_derive_lexique=_num(st, "max_derive_lexique", base.max_derive_lexique, path),
        min_entites=int(_num(st, "min_entites", base.min_entites, path)),
        instantanes_conserves=int(
            _num(d, "instantanes_conserves", base.instantanes_conserves, path, 1)
        ),
        rapports_dir=Path(str(d.get("rapports_dir", base.rapports_dir))),
    )


def slug(phrase: str) -> str:
    """Identifiant de grappe stable dérivé d'une phrase : `sujet-<mots>` (accents et ponctuation ôtés)."""
    import re

    from grappes.semantic.scoring import normalize

    words = re.sub(r"[^\w]+", " ", normalize(phrase)).split()[:6]
    return "sujet-" + "-".join(words) if words else "sujet"


def _subject_entry(entry: Any, path: Path) -> tuple[str, str]:
    """Un sujet : une phrase, ou `{nom: identifiant, phrase: "..."}`. Retourne (phrase, identifiant)."""
    if isinstance(entry, str) and entry.strip():
        return entry, ""
    if isinstance(entry, dict) and isinstance(entry.get("phrase"), str) and entry["phrase"].strip():
        nom = entry.get("nom")
        if nom is not None and (
            not isinstance(nom, str) or not re.fullmatch(r"[\w.-]+", nom.strip())
        ):
            raise ConfigError(
                f"{path}: `nom` d'un sujet : lettres, chiffres, _ . - seulement ({nom!r})"
            )
        return entry["phrase"], nom.strip() if nom else ""
    raise ConfigError(f"{path}: un sujet est une phrase, ou {{nom, phrase}}")


def load_subjects(config_dir: str | Path = "config") -> Subjects:
    """`config/sujets.yaml` : `sujets: ["une phrase", ...]`. Absent = aucun sujet."""
    path = Path(config_dir) / "sujets.yaml"
    if not path.exists():
        return Subjects()
    raw = _yaml(path).get("sujets") or []
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: `sujets` doit être une liste")
    seen: dict[str, Subject] = {}
    for entry in raw:
        phrase, sid = _subject_entry(entry, path)
        phrase = " ".join(phrase.split())
        sid = sid or slug(phrase)
        if sid in seen and seen[sid].phrase != phrase:
            n = 2
            while f"{sid}-{n}" in seen:
                n += 1
            sid = f"{sid}-{n}"
        seen.setdefault(sid, Subject(sid, phrase))
    return Subjects(tuple(seen.values()))
