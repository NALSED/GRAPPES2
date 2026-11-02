"""Collecte et ingestion automatique (FINAL.md §§ 6 et 7).

Pour chaque sujet : la multiprise de recherche rapporte des candidats ; un candidat est ingéré
automatiquement s'il est **public**, respecte les règles générales de visite, n'est pas dans les
**INTERDITS** et atteint une **proximité minimale** avec un sujet. Pas de validation humaine.
Profondeur d'exploration et nombres de requêtes, de pages, de nouvelles sources et d'observations par
cycle sont plafonnés. Le recoupement continue de décider : une source non recoupée reste « probable »
au mieux. Les réseaux sociaux ne sont jamais lus (leurs liens sont des citations).
"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from grappes.autonomy import Autonomy
from grappes.canon import canonical_domain, canonical_url, link_target
from grappes.config import Config
from grappes.fetch import PageFetcher
from grappes.ingestion import get_fetcher
from grappes.ingestion.base import make_obs_id, parse_ts
from grappes.models import Grappe, Observation, SourceRef
from grappes.search.config import SearchConfig
from grappes.search.engine import run_search
from grappes.semantic.scoring import normalize, score_detail, score_proximity
from grappes.storage.db import Database

MAX_PAGES_PER_DOMAIN = 5  # par cycle : explorer un site, pas l'aspirer
TOP_TERMS = 4  # termes du lexique combinés par deux avec la phrase pour varier les requêtes


@dataclass(slots=True)
class Ingested:
    url: str
    domain: str
    subject: str  # identifiant de la grappe du sujet
    score: float
    depth: int
    new_source: bool
    ref: str = ""  # clé de la source ingérée (ex. web:lemonde.fr, wikipedia:fr/udf)


@dataclass(slots=True)
class CollectReport:
    dry_run: bool = False
    requests: int = 0
    candidates: int = 0
    pages_attempted: int = 0
    observations_new: int = 0
    ingested: list[Ingested] = field(default_factory=list)
    refused: Counter = field(default_factory=Counter)  # motif -> nombre
    providers: dict[str, str] = field(default_factory=dict)
    blocked_domains: list[str] = field(default_factory=list)
    skipped_subjects: list[str] = field(default_factory=list)
    stopped: bool = False

    @property
    def new_sources(self) -> list[str]:
        return [i.domain for i in self.ingested if i.new_source]


def lexicon_terms(g: Grappe) -> dict[str, float]:
    return {**g.lexicon.terms, **{k: -v for k, v in g.lexicon.negative.items()}}


def proximity(text: str, grappe: Grappe) -> float:
    """Proximité (0 à 100) d'un texte avec le lexique d'un sujet : même fonction que le scoring."""
    return score_proximity(text, lexicon_terms(grappe), scale=grappe.scale) if text.strip() else 0.0


def stem(term: str) -> str:
    """Radical grossier : un pluriel en -s ou -x se ramène au singulier (« blocages » -> « blocage »)."""
    t = normalize(term).lstrip("#")
    return t[:-1] if len(t) >= 5 and t.endswith(("s", "x")) and not t.endswith(("ss", "us")) else t


def search_queries(g: Grappe, top_terms: int = TOP_TERMS) -> list[str]:
    """Requêtes d'un sujet : sa phrase, puis ses meilleurs termes combinés par deux. Un mot seul ramène du
    bruit, et deux formes du même mot (« acteur acteurs ») ne diversifient rien : un seul terme par radical
    (constaté sur les premières données réelles)."""
    by_stem: dict[str, str] = {}
    for t in sorted(g.lexicon.terms, key=lambda t: (-g.lexicon.terms[t], len(t), t)):
        by_stem.setdefault(stem(t), t)
    top = list(by_stem.values())[:top_terms]
    pairs = [" ".join(top[i : i + 2]) for i in range(0, len(top) - 1, 2)]
    if len(top) >= 2 and len(top) % 2:  # un terme resté seul se joint au premier
        pairs.append(f"{top[-1]} {top[0]}")
    return list(dict.fromkeys([g.subject, *pairs]))


def distinct_terms(text: str, grappe: Grappe) -> int:
    """Nombre de radicaux DIFFÉRENTS du lexique présents dans le texte : un mot répété ne fait pas un sujet."""
    if not text.strip():
        return 0
    kw = {k: v for k, v in lexicon_terms(grappe).items() if v > 0}
    return len({stem(k) for k in score_detail(text, kw, scale=grappe.scale).matched})


def link_relevant(anchor: str, url: str, subjects: list[Grappe]) -> bool:
    """Un lien n'est suivi que si son texte ou son adresse touche au sujet : les liens de pied de page
    (cookies, mentions légales, applications) ne sont pas explorés."""
    words = re.sub(r"[^\w]+", " ", urlsplit(url).path)
    return any(proximity(f"{anchor} {words}", g) > 0 for g in subjects)


def best_subject(
    text: str, subjects: list[Grappe], min_terms: int = 1
) -> tuple[Grappe | None, float]:
    """Sujet le plus proche du texte et sa proximité ; 0 si moins de `min_terms` radicaux distincts."""
    best: tuple[Grappe | None, float] = (None, 0.0)
    for g in subjects:
        s = proximity(text, g)
        if s > best[1] and distinct_terms(text, g) >= min_terms:
            best = (g, s)
    return best


def collect(
    db: Database,
    cfg: Config,
    scfg: SearchConfig,
    auto: Autonomy,
    fetcher: PageFetcher,
    *,
    execute: bool,
    should_stop: Callable[[], bool] = lambda: False,
    log: bool = True,
    search_kwargs: dict | None = None,
) -> CollectReport:
    """Un passage de collecte. `execute` faux : aucun appel, rien d'écrit (dry-run)."""
    rep = CollectReport(dry_run=not execute)
    subjects = [g for g in cfg.grappes if g.subject]
    ready = [g for g in subjects if g.lexicon.terms]
    rep.skipped_subjects = [g.grappe_id for g in subjects if not g.lexicon.terms]
    if not execute or not ready:
        return rep

    # 1. recherche : phrase + meilleurs termes de chaque sujet, quota de requêtes partagé
    found: dict[str, tuple[float, str, object]] = {}  # url -> (score, id de grappe, candidat)
    # résultats qui SONT le document (flux RSS, Wikipédia), un seul exemplaire par article
    docs_by_key: dict[str, tuple[float, str, object]] = {}
    used = 0
    for g in ready:
        remaining = max(auto.requetes_par_cycle - used, 0)
        if remaining == 0 or should_stop():
            break
        res = run_search(
            replace(scfg, quota_total=min(scfg.quota_total, remaining)),
            search_queries(g, auto.requetes_termes),
            execute=True,
            sujet=g.subject,
            journaliser=log,
            **(search_kwargs or {}),
        )
        used += res.requests
        rep.providers.update(res.status)
        for m in res.raw:  # un candidat par résultat (URL), pas par entité fusionnée
            rep.candidates += 1
            reason = (
                _screen_doc(m, scfg, fetcher)
                if m.doc is not None
                else _screen_candidate(m, scfg, fetcher)
            )
            if reason:
                rep.refused[reason] += 1
                continue
            owner, score = best_subject(m.text, ready, auto.ingestion_termes_min)
            if owner is None or score < auto.seuil_ingestion:
                rep.refused["proximité insuffisante avec les sujets"] += 1
                continue
            if (
                m.doc is not None
            ):  # le même article revient pour plusieurs requêtes : un seul exemplaire
                key = m.url or f"{m.doc['ecosysteme']}:{m.doc['source']}"
                if key not in docs_by_key or score > docs_by_key[key][0]:
                    docs_by_key[key] = (score, owner.grappe_id, m)
                continue
            if m.url not in found or score > found[m.url][0]:
                found[m.url] = (score, owner.grappe_id, m)
    rep.requests = used
    docs = list(docs_by_key.values())

    # 2a. documents déjà en main (flux RSS, Wikipédia) : ingérés tels quels sous leur propre écosystème,
    #     sans récupérer de page (le fournisseur les a déjà livrés, robots.txt respecté s'il s'agit d'un flux).
    #     Le plafond de nouvelles sources est PARTAGÉ ÉQUITABLEMENT entre écosystèmes (sinon les 8 premiers
    #     articles Wikipédia prennent tout et aucune page web n'entre : plus de recoupement entre écosystèmes).
    known_refs = {r[0] for r in db.conn.execute("SELECT ref_key FROM sources")}
    new_refs: set[str] = set()
    new_domains: set[str] = set()  # nouvelles sources web (pages), comptées avec les précédentes
    cap = auto.nouvelles_sources_par_cycle
    ecosystems = {m.doc["ecosysteme"] for _s, _g, m in docs} | ({"web"} if found else set())
    share = max(1, cap // len(ecosystems)) if ecosystems else cap
    web_reserve = share if found else 0  # des places gardées pour les pages web
    by_eco: Counter = Counter()
    deferred: list = []

    def add_doc(score: float, gid: str, m, limited: bool) -> None:
        d = m.doc
        ref = f"{d['ecosysteme']}:{d['source']}"
        if rep.observations_new >= auto.elements_par_cycle:
            rep.refused["plafond d'observations par cycle atteint"] += 1
            return
        is_new = ref not in known_refs and ref not in new_refs
        total = len(new_refs) + len(new_domains)
        if is_new and total >= cap:
            rep.refused["plafond de nouvelles sources atteint"] += 1
            return
        if (
            is_new
            and limited
            and (by_eco[d["ecosysteme"]] >= share or len(new_refs) >= cap - web_reserve)
        ):
            deferred.append(
                (score, gid, m)
            )  # sa part est prise : une seconde chance s'il reste des places
            return
        if (
            m.url
            and db.conn.execute("SELECT 1 FROM observations WHERE raw_ref=?", (m.url,)).fetchone()
        ):
            rep.refused["document déjà ingéré"] += 1
            return
        obs = Observation(
            obs_id=make_obs_id(d["ecosysteme"], f"{d['source']}|{m.url}", m.text),
            source=SourceRef(d["ecosysteme"], d["source"]),
            ts=parse_ts(m.date) if m.date else None,
            text=m.text,
            links=tuple(d["liens"]),
            raw_ref=m.url or None,
            meta={"nom": d["nom"]} if d["nom"] else {},
        )
        added = db.add_observations([obs], {ref: cfg.sources.weight(ref)})
        if not added:
            rep.refused["observation déjà connue"] += 1
            return
        db.set_origin(ref, "recherche")
        rep.observations_new += added
        if is_new:
            new_refs.add(ref)
            by_eco[d["ecosysteme"]] += 1
        rep.ingested.append(Ingested(m.url, d["source"], gid, round(score, 2), 0, is_new, ref))

    for score, gid, m in sorted(docs, key=lambda t: (-t[0], t[2].url)):
        if should_stop():
            rep.stopped = True
            break
        add_doc(score, gid, m, limited=True)

    # 2b. récupération et ingestion des pages, du plus proche au moins proche, jusqu'aux plafonds
    known_sources = {
        r[0] for r in db.conn.execute("SELECT ident FROM sources WHERE ecosystem='web'")
    }
    queue: deque[tuple[float, str, int]] = deque(
        (score, url, 0)
        for url, (score, _gid, _m) in sorted(found.items(), key=lambda kv: -kv[1][0])
    )
    web = get_fetcher("web")
    seen_urls: set[str] = set()
    per_domain: Counter = Counter()
    while queue:
        if should_stop():
            rep.stopped = True
            break
        if rep.pages_attempted >= auto.pages_par_cycle:
            rep.refused["plafond de pages par cycle atteint"] += len(queue)
            break
        if rep.observations_new >= auto.elements_par_cycle:
            rep.refused["plafond d'observations par cycle atteint"] += len(queue)
            break
        _, url, depth = queue.popleft()
        key = canonical_url(url)
        if key in seen_urls:
            continue
        seen_urls.add(key)
        domain = canonical_domain(key)
        if db.conn.execute("SELECT 1 FROM observations WHERE raw_ref=?", (key,)).fetchone():
            rep.refused["page déjà ingérée"] += 1
            continue
        if per_domain[domain] >= MAX_PAGES_PER_DOMAIN:
            rep.refused["plafond de pages par domaine atteint"] += 1
            continue
        is_new = domain not in known_sources and domain not in new_domains
        if is_new and len(new_domains) + len(new_refs) >= auto.nouvelles_sources_par_cycle:
            rep.refused["plafond de nouvelles sources atteint"] += 1
            continue
        rep.pages_attempted += 1
        per_domain[domain] += 1
        res = fetcher.fetch(url)
        if res.domain_blocked:
            rep.blocked_domains.append(domain)
        if not res.ok or res.record is None:
            rep.refused[res.reason or res.status] += 1
            continue
        obs, _ = web.parse_many([res.record])
        if not obs:
            rep.refused["page illisible"] += 1
            continue
        owner, score = best_subject(obs[0].text, ready, auto.ingestion_termes_min)
        if owner is None or score < auto.seuil_ingestion:
            rep.refused["page trop éloignée des sujets"] += 1
            continue
        weights = {o.source.key: cfg.sources.weight(o.source.key) for o in obs}
        added = db.add_observations(obs, weights)
        if not added:
            rep.refused["observation déjà connue"] += 1
            continue
        for o in obs:
            db.set_origin(o.source.key, "recherche" if depth == 0 else "exploration")
        rep.observations_new += added
        if is_new:
            new_domains.add(domain)
        rep.ingested.append(
            Ingested(key, domain, owner.grappe_id, round(score, 2), depth, is_new, f"web:{domain}")
        )
        if depth < auto.profondeur:  # exploration : liens web sortants qui touchent au sujet
            for item in res.record.get("links", []):
                link = item["href"]
                dom = canonical_domain(link)
                if (
                    link_target(link).startswith("web:")
                    and canonical_url(link) not in seen_urls
                    and per_domain[dom] < MAX_PAGES_PER_DOMAIN
                    and link_relevant(item.get("anchor", ""), link, ready)
                ):
                    queue.append((0.0, link, depth + 1))
    # 2c. seconde chance des documents dont la part était prise, s'il reste des places dans le plafond total
    for score, gid, m in deferred:
        if should_stop():
            rep.stopped = True
            break
        add_doc(score, gid, m, limited=False)
    return rep


def _screen_doc(m, scfg: SearchConfig, fetcher: PageFetcher) -> str | None:
    """Motif de refus d'un résultat-document (exclusion du fournisseur, interdits), ou `None`."""
    if m.doc["exclu"]:
        return m.doc["exclu"]
    spec = scfg.providers.get(m.provider)
    if spec is not None and not spec.public:
        return "fournisseur non public"
    return (fetcher.rules.check_url(m.url) if m.url else None) or fetcher.rules.check_text(m.text)


def _screen_candidate(m, scfg: SearchConfig, fetcher: PageFetcher) -> str | None:
    """Motif de refus d'un candidat avant toute requête, ou `None` s'il peut être visité."""
    if m.ecosystem != "web" or not m.url:
        return "réseau social ou identité sans page : citation seulement, jamais lu"
    spec = scfg.providers.get(m.provider)
    if spec is not None and not spec.public:
        return "fournisseur non public"
    return fetcher.rules.check_url(m.url) or fetcher.rules.check_text(m.text)
