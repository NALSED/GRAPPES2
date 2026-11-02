"""Moteur de recherche : liste blanche, dry-run, quotas, fusion des candidats, CSV."""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from grappes import avancement
from grappes.clustering.export import safe_cell
from grappes.search import http as _http  # noqa: F401  (enregistre le type `http`)
from grappes.search import mcpclient as _mcpclient  # noqa: F401  (enregistre le type `mcp`)
from grappes.search.base import (
    Journal,
    ProviderStop,
    ProviderUnavailable,
    QuotaExceeded,
    SearchContext,
    SearchError,
    get_provider_class,
)
from grappes.search.config import SearchConfig
from grappes.search.models import CSV_COLUMNS, Candidate, MergedCandidate, merge
from grappes.search.plugins import discover_plugins

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SearchReport:
    candidates: list[MergedCandidate] = field(default_factory=list)
    raw: list[Candidate] = field(default_factory=list)  # avant fusion : un par résultat (par URL)
    status: dict[str, str] = field(default_factory=dict)  # fournisseur -> issue
    executed: bool = False
    requests: int = 0


def split_keywords(text: str) -> list[str]:
    """Mots-clés saisis : séparés par des virgules ou des retours à la ligne, sans doublon."""
    parts = [p.strip() for chunk in text.splitlines() for p in chunk.split(",")]
    return list(dict.fromkeys(p for p in parts if p))


def run_search(
    cfg: SearchConfig,
    keywords: Iterable[str],
    *,
    providers: Iterable[str] | None = None,
    execute: bool = False,
    extra: Iterable[Candidate] = (),
    sujet: str = "",
    journaliser: bool = True,
    sleep: Callable[[float], None] | None = None,
    transport=None,
) -> SearchReport:
    """Interroge les fournisseurs autorisés pour chaque mot-clé.

    `extra` : candidats déjà connus (liens cités), fusionnés avec les résultats.

    Appels réels seulement si `execute` (c'est-à-dire `autonome`) ET fournisseur dans la liste
    blanche `cfg.actifs`. Sinon : dry-run (journal, aucun appel). Toute erreur d'un fournisseur
    est contenue : elle n'arrête ni les autres fournisseurs ni le cycle.
    """
    discover_plugins(cfg.plugins_dir)
    keywords = list(dict.fromkeys(k for k in keywords if k.strip()))
    wanted = list(providers) if providers is not None else list(cfg.actifs)
    real = bool(execute)
    ctx = SearchContext(
        execute=real,
        journal=Journal(cfg.journal if journaliser else None),
        delay_s=cfg.delai_s,
        quota_total=cfg.quota_total,
        sujet=sujet,
        **({"sleep": sleep} if sleep else {}),
    )
    report = SearchReport(executed=real)
    found: list[Candidate] = []
    avancement.signaler("plan", prises={p: len(keywords) for p in wanted if p in cfg.actifs and p in cfg.providers})
    for pid in wanted:
        if pid not in cfg.actifs or pid not in cfg.providers:
            report.status[pid] = "non autorisé (absent de la liste blanche `actifs`)"
            ctx.journal.log(pid, "", "-", "refusé : hors liste blanche")
            continue
        spec = cfg.providers[pid]
        try:
            cls = get_provider_class(spec.type)
            provider = (
                cls(pid, spec.options, ctx, transport=transport)
                if transport and spec.type == "http"
                else cls(pid, spec.options, ctx)
            )
        except SearchError as exc:
            report.status[pid] = f"inactif : {exc}"
            ctx.journal.log(pid, "", "-", f"inactif : {exc}")
            continue
        report.status[pid] = "ok" if real else "dry-run"
        avancement.signaler("prise_debut", prise=pid)
        avant = len(found)
        for kw in keywords:
            try:
                trouves = list(provider.search(kw, cfg.limite_par_mot_cle))
                found.extend(trouves)
                for t in trouves:  # quelle source a trouvé quoi (rapport d'avancement)
                    avancement.signaler("trouve", prise=pid, cle=f"{t.ecosystem}:{t.ident}")
            except ProviderUnavailable as exc:
                report.status[pid] = f"inactif : {exc}"
                ctx.journal.log(pid, kw, "-", f"inactif : {exc}")
                break
            except ProviderStop as exc:
                report.status[pid] = f"arrêté : {exc}"
                break
            except QuotaExceeded as exc:
                report.status[pid] = f"quota : {exc}"
                ctx.journal.log(pid, kw, "-", f"quota : {exc}")
                break
            except SearchError as exc:
                report.status[pid] = f"erreur : {exc}"
                ctx.journal.log(pid, kw, "-", f"erreur : {exc}")
            except Exception as exc:  # noqa: BLE001 - plugin défaillant : contenu, jamais fatal
                report.status[pid] = f"erreur : {type(exc).__name__}"
                ctx.journal.log(pid, kw, "-", f"erreur plugin : {type(exc).__name__}")
                log.warning("fournisseur %s : %s", pid, exc)
        avancement.signaler("prise_fin", prise=pid, trouves=len(found) - avant, noms=len(found), statut=report.status[pid])
    report.raw = found
    report.candidates = merge(found + list(extra))
    report.requests = ctx.used_total
    return report


def write_candidates_csv(
    path: str | Path, candidates: list[MergedCandidate], sep: str = ";"
) -> Path:
    """`candidats.csv` : écosystème, identifiant, URL, mot-clé d'origine, fournisseur(s), raison,
    date, extra (JSON). Cellules texte neutralisées contre l'injection de formule tableur."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=sep, lineterminator="\n")
        w.writerow(CSV_COLUMNS)
        for c in sorted(candidates, key=lambda c: c.key):
            w.writerow(
                [
                    safe_cell(c.ecosystem),
                    safe_cell(c.ident),
                    safe_cell(c.url),
                    safe_cell(" | ".join(c.keywords)),
                    safe_cell(" | ".join(c.providers)),
                    safe_cell(" | ".join(c.reasons)),
                    c.date,
                    safe_cell(json.dumps(c.extra, ensure_ascii=False, sort_keys=True))
                    if c.extra
                    else "",
                ]
            )
    return out
