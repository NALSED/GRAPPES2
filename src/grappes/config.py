"""Chargement et validation des YAML de configuration ; empreinte `config_hash`."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from grappes.canon import canonical_domain, canonical_handle
from grappes.ecosystem.crossref import CrossRefRule
from grappes.fiabilite import Fiabilite, FiabiliteError, load_fiabilite
from grappes.learned import (
    LearnedError,
    canonical_digest,
    learned_dir,
    load_learned_lexicon,
    load_learned_thresholds,
    load_seed,
)
from grappes.models import Grappe, Lexicon
from grappes.personnes import DEFAULT_INSTITUTION_HINTS


class ConfigError(ValueError):
    """Configuration invalide."""


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    top_n_observations: int = 5
    seuil_min: float = 40.0
    marge_ambiguite: float = 10.0
    niveau_confirme: float = 70.0
    niveau_probable: float = 40.0
    entity_types: dict[str, str] = field(
        default_factory=lambda: {
            "x": "person",
            "telegram": "group",
            "web": "site",
            "instagram": "person",
        }
    )
    csv_separator: str = ";"
    csv_bom: bool = False
    formule_confiance: str = "complementaire"  # ou "ponderee" (ancienne formule, D12)
    w_semantic: float = 0.50  # formule « ponderee » : w_s·S + w_e·E + w_a·A
    w_ecosystem: float = 0.30
    w_anchor: float = 0.20
    c_ecosystem: float = 0.6  # formule « complementaire » : S + (100 − S)·(c_e·E + c_a·A)/100
    c_anchor: float = 0.4
    crossref_floor: float = 60.0
    max_iter: int = 3
    contexte_min_citants: int | None = (
        None  # contexte des citations pour une entité sans texte propre : N citants minimum ; None = jamais
    )
    max_fichier_mo: int | None = 200  # taille maximale d'un .json (un .jsonl n'a pas de limite)
    retention_days: int | None = 90  # données brutes ; None = conservation illimitée (D30)
    redact_persons: bool = True  # entités de type `person` masquées dans les sorties (D30)
    resultat_confiance_min: float | None = None  # seuil de resultat.csv ; None = niveau_probable
    resultat_inclure_ambigus: bool = False
    resultat_inclure_personnes: bool = False  # si vrai : personnes en clair dans resultat.csv (D32)
    resultat_indices_institution: tuple[str, ...] = DEFAULT_INSTITUTION_HINTS
    resultat_suffixes: tuple[str, ...] = ("officiel", "official", "channel", "chaîne", "chaine")
    crossref: CrossRefRule = field(default_factory=CrossRefRule)


@dataclass(frozen=True, slots=True)
class SourcesConfig:
    defaults: dict[str, float] = field(default_factory=dict)
    overrides: dict[str, float] = field(default_factory=dict)

    def weight(self, key: str) -> float:
        if key in self.overrides:
            return self.overrides[key]
        return self.defaults.get(key.split(":", 1)[0], 1.0)


@dataclass(frozen=True, slots=True)
class Config:
    scoring: ScoringConfig
    sources: SourcesConfig
    grappes: tuple[Grappe, ...]
    config_hash: str
    telegram_names: dict[str, str] = field(
        default_factory=dict
    )  # nom affiché normalisé -> id public
    fiabilite: Fiabilite = field(
        default_factory=Fiabilite
    )  # code Admiralty (neutre si le fichier est absent)
    racine: Path | None = None  # dossier de configuration (registres, caches) ; hors empreinte


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: un objet YAML est attendu")
    return data


def _mapping(d: Any, where: str) -> dict[str, Any]:
    """`None` -> {} ; autre chose qu'un mapping -> ConfigError (jamais d'AttributeError)."""
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise ConfigError(f"{where}: un mapping est attendu")
    return d


def _finite(value: Any, where: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: nombre attendu ({value!r})") from exc
    if not math.isfinite(x):
        raise ConfigError(f"{where}: nombre fini attendu ({value!r})")
    return x


def _ref(value: Any, where: str) -> str:
    """Référence de source `ecosysteme:ident` canonicalisée (casse, `@`, `www.`)."""
    from grappes.ingestion import registered_ecosystems

    if not isinstance(value, str) or ":" not in value:
        raise ConfigError(f"{where}: référence `ecosysteme:ident` attendue ({value!r})")
    eco, ident = (part.strip() for part in value.split(":", 1))
    eco = eco.casefold()
    if eco not in registered_ecosystems():
        raise ConfigError(f"{where}: écosystème inconnu {eco!r} dans {value!r}")
    ident = canonical_domain(ident) if eco == "web" else canonical_handle(ident)
    if not ident:
        raise ConfigError(f"{where}: identifiant vide dans {value!r}")
    return f"{eco}:{ident}"


def _ref_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{where}: une liste est attendue")
    return tuple(dict.fromkeys(_ref(v, where) for v in value))


def _weights(d: Any, where: str, *, allow_negative: bool = False) -> dict[str, float]:
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise ConfigError(f"{where}: un mapping terme -> poids est attendu")
    out: dict[str, float] = {}
    for k, v in d.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(f"{where}: poids non numérique pour {k!r}")
        _finite(v, f"{where}: poids {k!r}")
        if v <= 0 and not allow_negative:
            raise ConfigError(f"{where}: poids {k!r} doit être > 0")
        out[str(k)] = float(v)
    return out


def load_scoring(path: Path) -> ScoringConfig:
    d = _load_yaml(path)
    csv = _mapping(d.get("csv"), f"{path}:csv")
    default = ScoringConfig()
    w = _mapping(d.get("poids_confiance"), f"{path}:poids_confiance")
    cc = _mapping(d.get("poids_complementaire"), f"{path}:poids_complementaire")
    cr = _mapping(d.get("crossref"), f"{path}:crossref")
    res = _mapping(d.get("resultat"), f"{path}:resultat")
    dr = default.crossref
    try:
        window = cr.get("window_days", dr.window_days)
        rule = CrossRefRule(
            min_citers=int(cr.get("min_citers", dr.min_citers)),
            min_ecosystems=int(cr.get("min_ecosystems", dr.min_ecosystems)),
            min_weight_sum=float(cr.get("min_weight_sum", dr.min_weight_sum)),
            min_citer_confidence=float(cr.get("min_citer_confidence", dr.min_citer_confidence)),
            window_days=None if window is None else int(window),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: crossref : {exc}") from exc
    try:
        cfg = ScoringConfig(
            top_n_observations=int(d.get("top_n_observations", default.top_n_observations)),
            seuil_min=float(d.get("seuil_min", default.seuil_min)),
            marge_ambiguite=float(d.get("marge_ambiguite", default.marge_ambiguite)),
            niveau_confirme=float(d.get("niveau_confirme", default.niveau_confirme)),
            niveau_probable=float(d.get("niveau_probable", default.niveau_probable)),
            entity_types={
                str(k): str(v)
                for k, v in _mapping(d.get("entity_types"), f"{path}:entity_types").items()
            }
            or default.entity_types,
            csv_separator=str(csv.get("separator", default.csv_separator)),
            csv_bom=bool(csv.get("bom", default.csv_bom)),
            formule_confiance=str(d.get("formule_confiance", default.formule_confiance)),
            c_ecosystem=float(cc.get("ecosysteme", default.c_ecosystem)),
            c_anchor=float(cc.get("ancrage", default.c_anchor)),
            w_semantic=float(w.get("semantique", default.w_semantic)),
            w_ecosystem=float(w.get("ecosysteme", default.w_ecosystem)),
            w_anchor=float(w.get("ancrage", default.w_anchor)),
            crossref_floor=float(d.get("crossref_floor", default.crossref_floor)),
            max_iter=int(d.get("max_iter", default.max_iter)),
            contexte_min_citants=(
                None
                if d.get("contexte_min_citants") in (None, 0)
                else int(d["contexte_min_citants"])
            ),
            max_fichier_mo=(
                None
                if d.get("max_fichier_mo", 200) in (None, 0)
                else int(d.get("max_fichier_mo", 200))
            ),
            retention_days=(
                None
                if d.get("retention_days", 90) in (None, 0)
                else int(d.get("retention_days", 90))
            ),
            redact_persons=bool(d.get("redact_persons", default.redact_persons)),
            resultat_confiance_min=(
                None if res.get("confiance_min") is None else float(res["confiance_min"])
            ),
            resultat_inclure_ambigus=bool(res.get("inclure_ambigus", False)),
            resultat_inclure_personnes=bool(res.get("inclure_personnes", False)),
            resultat_indices_institution=tuple(
                str(x) for x in res.get("indices_institution", DEFAULT_INSTITUTION_HINTS)
            ),
            resultat_suffixes=tuple(
                str(x).casefold() for x in res.get("suffixes", default.resultat_suffixes)
            ),
            crossref=rule,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if cfg.top_n_observations < 1:
        raise ConfigError(f"{path}: top_n_observations doit être >= 1")
    if (
        min(cfg.w_semantic, cfg.w_ecosystem, cfg.w_anchor) < 0
        or (cfg.w_semantic + cfg.w_ecosystem + cfg.w_anchor) <= 0
    ):
        raise ConfigError(f"{path}: poids_confiance invalides")
    if cfg.formule_confiance not in ("complementaire", "ponderee"):
        raise ConfigError(f"{path}: formule_confiance doit être complementaire ou ponderee")
    if (
        min(cfg.c_ecosystem, cfg.c_anchor) < 0
        or cfg.c_ecosystem + cfg.c_anchor > 1
        or cfg.c_ecosystem + cfg.c_anchor <= 0
    ):
        raise ConfigError(f"{path}: poids_complementaire invalides (somme dans ]0 ; 1])")
    floats = {
        "seuil_min": cfg.seuil_min,
        "marge_ambiguite": cfg.marge_ambiguite,
        "niveau_confirme": cfg.niveau_confirme,
        "niveau_probable": cfg.niveau_probable,
        "crossref_floor": cfg.crossref_floor,
        "poids_confiance.semantique": cfg.w_semantic,
        "poids_confiance.ecosysteme": cfg.w_ecosystem,
        "poids_confiance.ancrage": cfg.w_anchor,
        "poids_complementaire.ecosysteme": cfg.c_ecosystem,
        "poids_complementaire.ancrage": cfg.c_anchor,
        "crossref.min_weight_sum": cfg.crossref.min_weight_sum,
        "crossref.min_citer_confidence": cfg.crossref.min_citer_confidence,
    }
    for name, value in floats.items():
        _finite(value, f"{path}: {name}")
    if cfg.crossref.min_weight_sum <= 0:
        raise ConfigError(f"{path}: crossref.min_weight_sum doit être > 0")
    if cfg.crossref.window_days is not None and cfg.crossref.window_days < 1:
        raise ConfigError(f"{path}: crossref.window_days doit être >= 1 (ou null)")
    if cfg.retention_days is not None and cfg.retention_days < 1:
        raise ConfigError(f"{path}: retention_days doit être >= 1 (ou null)")
    if cfg.resultat_confiance_min is not None:
        _finite(cfg.resultat_confiance_min, f"{path}: resultat.confiance_min")
    if cfg.max_iter < 1:
        raise ConfigError(f"{path}: max_iter doit être >= 1")
    if cfg.crossref.min_citers < 1 or cfg.crossref.min_ecosystems < 1:
        raise ConfigError(f"{path}: crossref.min_citers et min_ecosystems doivent être >= 1")
    if len(cfg.csv_separator) != 1:
        raise ConfigError(f"{path}: csv.separator doit être un caractère")
    bad = set(cfg.entity_types.values()) - {"person", "group", "site"}
    if bad:
        raise ConfigError(f"{path}: types d'entité inconnus {sorted(bad)}")
    return cfg


def load_sources(path: Path) -> SourcesConfig:
    d = _load_yaml(path)
    return SourcesConfig(
        defaults=_weights(d.get("defaults"), f"{path}:defaults"),
        overrides=_weights(d.get("overrides"), f"{path}:overrides"),
    )


def _opposition_list(value: Any, where: str) -> tuple[str, ...]:
    """Identifiants de grappe (sans `:`) ou références de sources (`ecosysteme:ident`)."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{where}: une liste est attendue")
    out = [_ref(v, where) if isinstance(v, str) and ":" in v else str(v) for v in value]
    return tuple(dict.fromkeys(out))


def load_grappe(path: Path) -> Grappe:
    d = _load_yaml(path)
    gid = d.get("id")
    if not isinstance(gid, str) or not gid.strip():
        raise ConfigError(f"{path}: `id` manquant")
    lex = _mapping(d.get("lexicon"), f"{path}:lexicon")
    terms = _weights(lex.get("terms"), f"{path}:lexicon.terms")  # facultatif (FINAL.md § 3)
    negative = _weights(lex.get("negative"), f"{path}:lexicon.negative")
    scale = _finite(d.get("scale", 3.0), f"{path}: scale")
    if scale <= 0:
        raise ConfigError(f"{path}: scale doit être > 0")
    return Grappe(
        grappe_id=gid.strip(),
        label=str(d.get("label", gid)),
        lexicon=Lexicon(terms=terms, negative=negative),
        anchors=_ref_list(d.get("anchors"), f"{path}:anchors"),
        oppositions=_opposition_list(d.get("oppositions"), f"{path}:oppositions"),
        scale=scale,
    )


def _with_learned(g: Grappe, learned: dict) -> Grappe:
    """Ajoute les termes appris au lexique, sans jamais remplacer ni retirer un terme de Sednal."""
    extra = {
        t: lt.weight
        for t, lt in learned.items()
        if t not in g.lexicon.terms and t not in g.lexicon.negative
    }
    if not extra:
        return g
    lex = Lexicon(terms={**g.lexicon.terms, **extra}, negative=g.lexicon.negative)
    return replace(g, lexicon=lex, learned_terms=tuple(sorted(extra)))


def load_telegram_names(path: Path) -> dict[str, str]:
    """`config/telegram.yaml` : `equivalences: {"Nom affiché": identifiant_public}` (D25).

    La clé est normalisée (casse, accents, ponctuation) ; deux noms qui se normalisent pareil
    vers des identifiants différents sont refusés : rien ne doit être fusionné par erreur.
    """
    import re

    from grappes.canon import canonical_handle
    from grappes.semantic.scoring import normalize

    raw = _mapping(_load_yaml(path).get("equivalences"), f"{path}:equivalences")
    table: dict[str, str] = {}
    for name, ident in raw.items():
        if not isinstance(ident, str) or not canonical_handle(ident):
            raise ConfigError(f"{path}: identifiant public attendu pour {name!r}")
        key = re.sub(r"[^\w]+", " ", normalize(str(name))).strip()
        handle = canonical_handle(ident)
        if not key:
            raise ConfigError(f"{path}: nom affiché vide")
        if table.get(key, handle) != handle:
            raise ConfigError(f"{path}: le nom {name!r} désigne deux identifiants différents")
        table[key] = handle
    return table


def load_config(config_dir: str | Path = "config") -> Config:
    root = Path(config_dir)
    if not root.is_dir():
        raise ConfigError(f"dossier de configuration introuvable : {root}")
    scoring_path, sources_path = root / "scoring.yaml", root / "sources.yaml"
    scoring = load_scoring(scoring_path) if scoring_path.exists() else ScoringConfig()
    sources = load_sources(sources_path) if sources_path.exists() else SourcesConfig()
    files = sorted((root / "grappes").glob("*.yaml")) if (root / "grappes").is_dir() else []
    grappes = tuple(load_grappe(p) for p in files)
    ids = [g.grappe_id for g in grappes]
    if len(set(ids)) != len(ids):
        raise ConfigError("identifiants de grappe dupliqués")
    # sujets (une phrase chacun) : chacun devient une grappe, lexique amorcé automatiquement
    from grappes.autonomy import load_subjects

    subjects = load_subjects(root)
    subject_files = [root / "sujets.yaml"] if (root / "sujets.yaml").exists() else []
    for s in subjects.subjects:
        if s.id in ids:
            raise ConfigError(f"sujet {s.phrase!r}: identifiant de grappe déjà pris ({s.id})")
        ids.append(s.id)
    if not grappes and not subjects.subjects:
        raise ConfigError(
            f"aucune grappe ({root / 'grappes'}) ni sujet ({root / 'sujets.yaml'}) : "
            "fournir au moins un sujet en une phrase"
        )
    for g in grappes:
        for o in g.oppositions:
            if ":" not in o and o not in ids:
                raise ConfigError(f"{g.grappe_id}: opposition vers une grappe inconnue {o!r}")
    fiab_path = root / "fiabilite_sources.yaml"
    try:
        fiabilite = load_fiabilite(fiab_path)
    except FiabiliteError as exc:
        raise ConfigError(str(exc)) from exc
    tg_path = root / "telegram.yaml"
    telegram_names = load_telegram_names(tg_path) if tg_path.exists() else {}
    # fichiers « appris » (phase 9) : séparés, jamais prioritaires sur les termes de Sednal
    adir = learned_dir(root)
    learned_files = sorted(adir.glob("*.yaml")) if adir.is_dir() else []
    try:
        grappes += tuple(
            Grappe(
                grappe_id=s.id,
                label=s.phrase,
                lexicon=Lexicon(terms={t: lt.weight for t, lt in load_seed(root, s.id).items()}),
                subject=s.phrase,
            )
            for s in subjects.subjects
        )
        thresholds = load_learned_thresholds(root)
        grappes = tuple(_with_learned(g, load_learned_lexicon(root, g.grappe_id)) for g in grappes)
    except LearnedError as exc:
        raise ConfigError(str(exc)) from exc
    if thresholds:
        scoring = replace(scoring, **thresholds)
    # empreinte : contenu brut de tous les fichiers pris en compte, dans l'ordre
    h = hashlib.sha256()
    for p in (
        [p for p in (scoring_path, sources_path) if p.exists()]
        + files
        + subject_files
        + ([tg_path] if tg_path.exists() else [])
        + ([fiab_path] if fiab_path.exists() else [])
    ):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    try:
        for p in learned_files:  # fichiers appris : termes et poids seulement (voir learned.py)
            digest = canonical_digest(p)
            if (
                digest != b"[]"
            ):  # un fichier sans effet sur le scoring (ex. seul seuil d'ingestion) : ignoré
                h.update(p.name.encode())
                h.update(digest)
    except LearnedError as exc:
        raise ConfigError(str(exc)) from exc
    return Config(scoring, sources, grappes, h.hexdigest()[:16], telegram_names, fiabilite, root)


def config_summary(cfg: Config) -> str:
    return json.dumps(
        {"hash": cfg.config_hash, "grappes": [g.grappe_id for g in cfg.grappes]}, ensure_ascii=False
    )
