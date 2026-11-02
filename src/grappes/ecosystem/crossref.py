"""Citations et recoupement (le recoupement lui-même arrive en phase 4)."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from grappes.canon import canonical_domain, link_target, site_key
from grappes.models import Observation
from grappes.semantic.scoring import normalize


@dataclass(frozen=True, slots=True)
class Citation:
    citer: str  # clé canonique de la source citante
    cited: str  # clé canonique de l'élément cité (web:domaine, x:handle, ...)
    ecosystem: str  # écosystème de la source citante
    obs_id: str
    ts: datetime | None
    hypothesis: bool = False  # identité du citant ou du cité déduite d'un nom affiché (D25)
    reliability: str = ""  # fiabilité Admiralty du citant (A à F), fixée par `assign`
    credibility: int = 6  # crédibilité Admiralty (1 à 6) de l'élément cité, fixée par `assign`
    via: str = "lien"  # « lien » (lien explicite, mention) ou « texte » (nom exact d'une entité connue, D54)


TEXT_ECOSYSTEMS = ("rss", "web")  # écosystèmes dont le texte libre est lu pour des mentions (D54)
TEXT_CITABLE = (
    "web",
    "wikipedia",
    "rss",
)  # comptes et personnes : jamais reconnus par leur nom (D25)
MIN_NAME_LEN = 5  # un nom plus court (« Le », « ENS ») n'est jamais cherché dans un texte


def _same_site(a: str, b: str) -> bool:
    """Un flux et le site du même éditeur (`rss:x.fr`, `web:x.fr`) : jamais deux voix indépendantes."""
    ea, _, ia = a.partition(":")
    eb, _, ib = b.partition(":")
    return (
        ea in ("web", "rss")
        and eb in ("web", "rss")
        and canonical_domain(ia) == canonical_domain(ib)
    )


def name_index(names: dict[str, str] | None, known: set[str] | None = None) -> dict[str, str]:
    """Noms CONNUS -> clé de source : nom affiché normalisé et, pour un site, son domaine. Un nom trop court ou
    partagé par plusieurs sources (ambigu) est écarté. Aucun nom n'est inventé ni deviné (D54)."""
    found: dict[str, set[str]] = {}
    for ref, name in (names or {}).items():
        if (known is not None and ref not in known) or ref.partition(":")[0] not in TEXT_CITABLE:
            continue
        keys = {normalize(name).strip()}
        eco, _, ident = ref.partition(":")
        if eco in ("web", "rss"):
            keys.add(canonical_domain(ident))
        for k in keys:
            if len(k) >= MIN_NAME_LEN:
                found.setdefault(k, set()).add(ref)
    return {k: next(iter(v)) for k, v in found.items() if len(v) == 1}


def _name_regex(index: dict[str, str]) -> re.Pattern[str] | None:
    if not index:
        return None
    alt = "|".join(re.escape(k) for k in sorted(index, key=lambda k: (-len(k), k)))
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)")


def build_citations(
    observations: list[Observation], names: dict[str, str] | None = None
) -> list[Citation]:
    """Une citation par (observation, élément cité) ; auto-citations ignorées.

    Avec `names` (sources déjà connues), le texte libre des éléments RSS et des pages web peut aussi citer : un nom
    exact (normalisé, mot entier) ou le domaine d'une source DÉJÀ connue donne une citation `via="texte"`. Jamais de
    création d'entité : seules les clés de `names` peuvent être citées ; pas de correspondance approchée.

    Les liens citent la cible identifiable par `canon.link_target` (compte X, canal Telegram,
    compte Instagram, domaine web) ; les cibles non identifiables ne citent rien (D24).
    Tri stable pour la reproductibilité.
    """
    out: dict[tuple[str, str, str], Citation] = {}
    index = name_index(names)
    pattern = _name_regex(index)
    for o in observations:
        cited_keys = [m.key for m in o.mentions]
        cited_keys += [t for t in map(link_target, o.links) if t]
        for cited in cited_keys:
            if cited == o.source.key:
                continue
            hypo = o.source.key in o.hypothetical or cited in o.hypothetical
            out.setdefault(
                (o.source.key, cited, o.obs_id),
                Citation(o.source.key, cited, o.source.ecosystem, o.obs_id, o.ts, hypo),
            )
        if pattern is None or o.source.ecosystem not in TEXT_ECOSYSTEMS:
            continue
        for m in {index[x] for x in pattern.findall(normalize(o.text))}:
            if m == o.source.key or _same_site(m, o.source.key):
                continue  # un éditeur ne se cite pas lui-même (auto-corroboration écartée)
            hypo = o.source.key in o.hypothetical
            out.setdefault(
                (o.source.key, m, o.obs_id),
                Citation(o.source.key, m, o.source.ecosystem, o.obs_id, o.ts, hypo, via="texte"),
            )
    return [out[k] for k in sorted(out)]


@dataclass(frozen=True, slots=True)
class CrossRefRule:
    min_citers: int = 3  # X : nombre minimal de sources citantes distinctes
    min_ecosystems: int = 2  # croisement d'écosystèmes
    min_weight_sum: float = 3.0  # somme minimale des poids des citants
    min_citer_confidence: float = 50.0  # un citant ne compte que s'il est déjà membre
    window_days: int | None = 180  # fenêtre temporelle (filtre appliqué en amont)


@dataclass(frozen=True, slots=True)
class ValidationRecord:
    cited: str
    grappe_id: str
    citers: tuple[str, ...]
    ecosystems: tuple[str, ...]
    weight_sum: float
    validated: bool
    reasons: tuple[str, ...]  # explicabilité : pourquoi validé / pourquoi refusé


def filter_window(citations: list[Citation], window_days: int | None) -> list[Citation]:
    """Garde les citations dans la fenêtre, relative à la plus récente (pas à l'horloge)."""
    stamped = [c.ts for c in citations if c.ts is not None]
    if window_days is None or not stamped:
        return citations
    cut = max(stamped).timestamp() - window_days * 86400
    return [c for c in citations if c.ts is None or c.ts.timestamp() >= cut]


def validate_crossref(
    citations: list[Citation],
    grappe_id: str,
    is_member: Callable[[str], bool],
    weight: Callable[[str], float],
    rule: CrossRefRule,
    text_weight: Callable[[str], float] | None = None,
) -> dict[str, ValidationRecord]:
    """Validation par recoupement pour une grappe. Un citant = une voix ; un record par
    élément cité (y compris les refus, avec leurs raisons). `is_member` reflète l'état de
    l'itération précédente : un élément validé ici ne sert pas de citant dans la même passe."""
    by_cited: dict[str, set[str]] = {}
    text_only: dict[
        str, set[str]
    ] = {}  # citants qui ne citent QUE par mention textuelle (voix plus légère)
    linked: set[tuple[str, str]] = {(c.cited, c.citer) for c in citations if c.via != "texte"}
    for c in citations:
        if c.citer != c.cited:  # auto-citations ignorées
            by_cited.setdefault(c.cited, set()).add(c.citer)
            if (c.cited, c.citer) not in linked:
                text_only.setdefault(c.cited, set()).add(c.citer)
    out: dict[str, ValidationRecord] = {}
    for cited in sorted(by_cited):
        raw = by_cited[cited]
        tw = text_weight or weight

        def w_of(x: str, cited=cited, tw=tw) -> float:
            return tw(x) if x in text_only.get(cited, ()) else weight(x)

        members = sorted(x for x in raw if is_member(x))
        # une voix par ÉDITEUR (D58) : `agency.x.fr`, `x.fr` et le flux `rss:x.fr` ne sont pas des voix indépendantes ;
        # on garde le citant de plus fort poids, et les écosystèmes ne se lisent que sur ces représentants.
        by_publisher: dict[str, str] = {}
        for x in members:
            eco, _, ident = x.partition(":")
            key = site_key(ident) if eco in ("web", "rss") else x
            best_x = by_publisher.get(key)
            if best_x is None or w_of(x) > w_of(best_x):
                by_publisher[key] = x
        citers = tuple(sorted(by_publisher.values()))
        merged = len(members) - len(citers)
        ecos = tuple(sorted({x.split(":", 1)[0] for x in citers}))
        weights = [w_of(x) for x in citers]
        wsum = round(sum(weights), 6)
        # Effectif minimal de citants DÉDUIT des poids, pas inventé : le poids total à atteindre ne peut venir de
        # moins de citants que min_weight_sum / (meilleur poids présent), jamais moins que le nombre d'écosystèmes
        # exigés (donc au moins 2 citants), jamais plus que min_citers. Poids uniformes = comportement historique.
        best = max(weights, default=1.0)
        need = min(
            rule.min_citers,
            max(rule.min_ecosystems, 2, math.ceil(rule.min_weight_sum / best - 1e-9)),
        )
        reasons: list[str] = []
        if len(citers) < need:
            reasons.append(f"citants membres {len(citers)} < {need}")
        if len(ecos) < rule.min_ecosystems:
            reasons.append(f"écosystèmes {len(ecos)} < {rule.min_ecosystems}")
        if wsum < rule.min_weight_sum:
            reasons.append(f"poids {wsum:g} < {rule.min_weight_sum:g}")
        if len(raw) > len(members):
            reasons.append(f"{len(raw) - len(members)} citant(s) non membres ignorés")
        if merged:
            reasons.append(f"{merged} citant(s) fusionné(s) : même éditeur qu'un autre citant")
        validated = not any("<" in r for r in reasons)
        if validated:
            reasons.insert(0, "validé : seuils atteints")
        out[cited] = ValidationRecord(
            cited, grappe_id, citers, ecos, wsum, validated, tuple(reasons)
        )
    return out
