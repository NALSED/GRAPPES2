"""Orchestration d'un cycle : collecte -> ingestion -> score -> recoupement -> assignation ->
apprentissage -> recalibrage -> export -> rapport (FINAL.md § 8).

* Idempotent (mêmes données = mêmes résultats), journalisé (`donnees/cycles.jsonl`), verrouillé
  (un seul cycle à la fois), arrêtable à tout instant (`autonome: false`, fichier STOP).
* `autonome: false` (défaut) : simulation. Rien n'est appelé ni écrit : ni base, ni fichier, ni journal.
* Un instantané est pris avant tout cycle réel. Une erreur, une demande d'arrêt ou un indicateur de
  stabilité au-delà de son seuil RESTAURE cet instantané et le dit dans le rapport.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from grappes import avancement
from grappes.amorcage import amorcer
from grappes.autonomy import Autonomy, load_autonomy
from grappes.clustering.assign import AMBIGU, CLASSE
from grappes.clustering.export import export_csv
from grappes.classement.production import appliquer as appliquer_classement
from grappes.clustering.resultat import write_resultat
from grappes.config import Config, load_config
from grappes.cycle.collect import collect
from grappes.cycle.learn import (
    adjust_ingestion,
    confirmed_by_grappe,
    learn_grappe,
    recalibrate,
    trusted_refs,
)
from grappes.cycle.metrics import Indicators, compare, run_metrics
from grappes.cycle.report import CycleData, render, write_report
from grappes.cycle.state import (
    CycleBusy,
    Snapshot,
    append_log,
    cycle_lock,
    find_snapshot,
    next_cycle_number,
    prune_snapshots,
    read_log,
    restore_snapshot,
    take_snapshot,
)
from grappes.fetch import PageFetcher
from grappes.learned import (
    load_learned_lexicon,
    save_ingestion_threshold,
    save_learned_lexicon,
    save_learned_thresholds,
)
from grappes.models import EntityType
from grappes.pipeline import places_for, reliability_for, run
from grappes.registres import load_registres
from grappes.registres import update as registres_update
from grappes.search.base import Journal
from grappes.search.config import SearchConfig, load_search_config
from grappes.search.engine import run_search
from grappes.search.http import urllib_transport
from grappes.storage.db import Database
from grappes.tranco import ensure as ensure_tranco

OK, CANCELLED, STOPPED, FAILED, DRY, BUSY = "ok", "annule", "arret", "erreur", "dry-run", "occupe"
EXIT = {OK: 0, DRY: 0, FAILED: 1, BUSY: 3, STOPPED: 4, CANCELLED: 5}


class StopRequested(Exception):
    """Le fichier STOP est apparu pendant le cycle."""


@dataclass(slots=True)
class CycleOutcome:
    status: str
    cycle_id: str = ""
    n: int = 0
    run_id: str | None = None
    report_path: Path | None = None
    report: str = ""
    indicators: Indicators | None = None
    data: CycleData | None = None
    extra: dict = field(default_factory=dict)

    @property
    def code(self) -> int:
        return EXIT[self.status]


@dataclass(slots=True)
class Paths:
    config: Path
    db: Path
    out: Path
    data: Path  # dossier `donnees/` : verrou, journal des cycles, instantanés

    @property
    def lock(self) -> Path:
        return self.data / "cycle.lock"

    @property
    def log(self) -> Path:
        return self.data / "cycles.jsonl"

    @property
    def snapshots(self) -> Path:
        return self.data / "instantanes"


# --- utilitaires ----------------------------------------------------------------------------------


def _label_map(cfg: Config) -> dict[str, str]:
    return {g.grappe_id: (g.subject or g.label or g.grappe_id) for g in cfg.grappes}


def _subjects_info(cfg: Config) -> list[tuple[str, str, int]]:
    return [
        (g.grappe_id, g.subject, len(g.lexicon.terms) - len(g.learned_terms))
        for g in cfg.grappes
        if g.subject
    ]


def _display(result, cfg: Config, db_names: dict[str, str]) -> Callable[[str], str]:
    types = {e.primary_ref: e.type for e in result.entities}

    def name(ref: str) -> str:
        if cfg.scoring.redact_persons and types.get(ref) is EntityType.PERSON:
            return "une personne (masquée)"
        return db_names.get(ref) or ref.split(":", 1)[-1]

    return name


def _main_members(result) -> dict[str, set[str]]:
    ents = {e.entity_id: e.primary_ref for e in result.entities}
    out: dict[str, set[str]] = {}
    for m in result.memberships:
        if m["status"] in (CLASSE, AMBIGU):
            out.setdefault(m["grappe_id"], set()).add(ents[m["entity_id"]])
    return out


def _hypothetical(db: Database, result) -> tuple[int, int]:
    n_cit = sum(1 for c in result.citations if c.hypothesis)
    refs = {c.citer for c in result.citations if c.hypothesis} | {
        c.cited for c in result.citations if c.hypothesis
    }
    return n_cit, len([r for r in refs if r.startswith("telegram:")])


def _seed_subjects(
    cfg, scfg, auto, paths, n, search_kwargs, used_requests
) -> tuple[Config, list[str], int, dict]:
    """Amorce les sujets qui n'ont pas encore de lexique. Retourne (config rechargée, amorcés, requêtes, états)."""
    seeded, used, statuses = [], 0, {}
    for g in cfg.grappes:
        if not g.subject or g.lexicon.terms:
            continue
        left = max(auto.requetes_par_cycle - used_requests - used, 0)
        if left == 0:
            break
        res = run_search(
            replace(scfg, quota_total=min(scfg.quota_total, left)),
            [g.subject],
            execute=True,
            sujet=g.subject,
            **search_kwargs,
        )
        used += res.requests
        statuses.update(res.status)
        seed = amorcer(
            g.grappe_id, g.subject, paths.config, [c.text for c in res.raw],
            auto.amorcage_max_termes, auto.amorcage_poids, n,
        )  # fmt: skip
        if seed.terms:
            seeded.append(g.grappe_id)
    return (load_config(paths.config) if seeded else cfg), seeded, used, statuses


# --- cycle réel ------------------------------------------------------------------------------------


def _update_fiabilite(db, cfg, scfg, transport, now) -> dict[str, object]:
    """Registres officiels et classement Tranco : mises à jour réseau bornées (caches), puis résumé pour le rapport.
    Une panne n'arrête jamais le cycle : sans cache, les sources restent à leur lettre statique."""
    out: dict[str, object] = {}
    if cfg.racine is None or not cfg.fiabilite.enabled:
        return out
    transport = transport or urllib_transport
    journal = Journal(scfg.journal)
    reg_path = cfg.racine / "registres.yaml"
    stats = {"requetes": 0}
    if reg_path.exists():
        try:
            rcfg = load_registres(reg_path)
            stats = registres_update(db, rcfg, now, transport, journal)
        except Exception as exc:  # noqa: BLE001
            journal.log("registres", "-", "-", f"erreur {type(exc).__name__}")
    tr = cfg.fiabilite.tranco
    if tr.get("actif"):
        meta = ensure_tranco(tr["dossier"], now, transport, journal, tr["cache_jours"], tr["api"])
        out["tranco"] = (
            f"liste {meta.get('liste_id', '?')} du {meta.get('cree_le', '?')} (tranco-list.eu)"
            if meta
            else "indisponible (sources web restent en D)"
        )
    rel = reliability_for(db, cfg)
    letters = Counter(
        rel.letter(r["ref_key"]) for r in db.conn.execute("SELECT ref_key FROM sources")
    )
    out["lettres"] = ", ".join(f"{k} : {v}" for k, v in sorted(letters.items())) or "aucune source"
    out["requetes_registres"] = stats.get("requetes", 0)
    out["en_a"] = len(rel.registre)
    return out


def run_cycle(
    config_dir: str | Path = "config",
    db_path: str | Path = "donnees/grappes.db",
    out_dir: str | Path = "sorties",
    data_dir: str | Path = "donnees",
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    search_transport=None,
    fetch_transport=None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    allow_private: bool = False,
) -> CycleOutcome:
    paths = Paths(Path(config_dir), Path(db_path), Path(out_dir), Path(data_dir))
    auto = load_autonomy(paths.config)
    now = now or datetime.now(UTC)
    if auto.stop_file.exists():
        return CycleOutcome(STOPPED, extra={"reason": f"arrêt demandé : {auto.stop_file} existe"})
    if dry_run or not auto.autonome:
        return _simulate(paths, auto, now, forced=not dry_run)
    try:
        with cycle_lock(paths.lock):
            return _execute(
                paths, auto, now, search_transport, fetch_transport, sleep, monotonic, allow_private
            )
    except CycleBusy as exc:
        return CycleOutcome(BUSY, extra={"reason": str(exc)})


def _execute(
    paths, auto, now, search_transport, fetch_transport, sleep, monotonic, allow_private
) -> CycleOutcome:
    n = next_cycle_number(paths.log)
    cycle_id = f"C{n:04d}-{now.strftime('%Y%m%dT%H%M%S')}"
    snap = take_snapshot(paths.snapshots, cycle_id, paths.db, paths.config, paths.out)
    data = CycleData(n, cycle_id, now, "réel", OK)
    db: Database | None = None
    cfg: Config | None = None
    scfg: SearchConfig | None = None
    try:
        cfg = load_config(paths.config)
        scfg = load_search_config(paths.config)
        data.grappe_labels = _label_map(cfg)
        data.include_persons = cfg.scoring.resultat_inclure_personnes
        search_kwargs = {"transport": search_transport} if search_transport else {}

        def check_stop() -> None:
            if auto.stop_file.exists():
                raise StopRequested

        check_stop()
        avancement.signaler("budget", requetes=auto.requetes_par_cycle, pages=auto.pages_par_cycle)
        avancement.signaler("etape", etape="recherche")
        db = Database(paths.db)
        before = run_metrics(db, db.latest_run_id(), cfg)
        data.before = before
        # amorçage des sujets sans lexique (la phrase seule, jamais de mots imposés)
        cfg, data.seeded, used, seed_status = _seed_subjects(
            cfg, scfg, auto, paths, n, search_kwargs, 0
        )
        data.grappe_labels = _label_map(cfg)
        data.subjects = _subjects_info(cfg)
        check_stop()
        if cfg.scoring.retention_days:
            data.purged = db.purge_older_than(cfg.scoring.retention_days)
        fetcher = PageFetcher(
            auto, Journal(scfg.journal), transport=fetch_transport, sleep=sleep, monotonic=monotonic, allow_private=allow_private
        )  # fmt: skip
        budget = replace(auto, requetes_par_cycle=max(auto.requetes_par_cycle - used, 0))
        data.collect = collect(
            db,
            cfg,
            scfg,
            budget,
            fetcher,
            execute=True,
            should_stop=check_stop,
            search_kwargs=search_kwargs,
        )
        data.collect.requests += used
        data.collect.providers.update(seed_status)
        if data.collect.stopped:
            raise StopRequested
        avancement.signaler("etape", etape="verification")
        data.fiabilite = _update_fiabilite(db, cfg, scfg, search_transport, now)
        run_id, res = run(db, cfg)
        # apprentissage (confirmées uniquement) et recalibrage (bornés)
        observations = db.load_observations()
        confirmed = confirmed_by_grappe(res, auto)
        trusted = trusted_refs(res, cfg.grappes, db.origins())
        members = _main_members(res)
        changed_files = False
        n_learned = 0
        if auto.apprentissage_actif:
            for g in cfg.grappes:
                existing = load_learned_lexicon(paths.config, g.grappe_id)
                gl = learn_grappe(
                    g,
                    res,
                    observations,
                    confirmed.get(g.grappe_id, set()),
                    trusted,
                    existing,
                    auto,
                    n,
                    members.get(g.grappe_id, set()),
                )
                data.learning.append(gl)
                if gl.terms != existing:
                    save_learned_lexicon(paths.config, g.grappe_id, gl.terms)
                    changed_files = changed_files or {t: v.weight for t, v in gl.terms.items()} != {
                        t: v.weight for t, v in existing.items()
                    }
                n_learned += len(gl.terms)
        data.recalibration = recalibrate(res, cfg, auto, confirmed)
        if data.recalibration.changed:
            save_learned_thresholds(paths.config, data.recalibration.after, n)
            changed_files = True
        check_stop()
        if changed_files:
            cfg = load_config(paths.config)
            run_id, res = run(db, cfg)
            n_learned = sum(len(g.learned_terms) for g in cfg.grappes)
        else:
            n_learned = sum(len(g.learned_terms) for g in cfg.grappes)
        after = run_metrics(db, run_id, cfg)
        data.after = after
        if auto.seuil_ingestion_auto and data.collect and not data.collect.dry_run:
            # pertinence observée : part des sources ingérées ce cycle qui sont ensuite rangées dans un sujet
            c = data.collect
            n_prox = sum(n for k, n in c.refused.items() if k.startswith("proximité insuffisante"))
            data.ingestion = adjust_ingestion(
                auto, [i.ref for i in c.ingested], set(after.main), c.candidates, n_prox
            )
            if data.ingestion.changed:
                save_ingestion_threshold(paths.config, data.ingestion.after, n)
        n_base = sum(len(g.lexicon.terms) - len(g.learned_terms) for g in cfg.grappes)
        data.indicators = compare(before, after, auto, n_learned, n_base)
        data.hypothetical_citations, data.hypothetical_sources = _hypothetical(db, res)
        name = _display(res, cfg, db.load_display_names())
        data.changed = [
            (name(r), data.grappe_labels.get(before.main[r], before.main[r]), data.grappe_labels.get(after.main[r], after.main[r]))
            for r in sorted(after.main)
            if r in before.main and before.main[r] != after.main[r]
        ]  # fmt: skip
        if data.indicators.exceeded:
            return _rollback(
                paths, db, snap, data, "Indicateur de stabilité au-delà de son seuil.", CANCELLED
            )
        avancement.signaler("etape", etape="sortie")
        export_csv(res, cfg, run_id, paths.out)
        write_resultat(res, cfg, paths.out, auto=auto, places=places_for(db, cfg))
        appliquer_classement(db, cfg, paths.config, paths.out, paths.data, auto.export_tsites_pays_defaut)
        prune_snapshots(paths.snapshots, auto.instantanes_conserves)
        avancement.signaler("fin", statut=OK)
        return _finish(paths, auto, data, run_id, db)
    except StopRequested:
        return _rollback(
            paths, db, snap, data, "Arrêt demandé (fichier STOP) pendant le cycle.", STOPPED
        )
    except (
        KeyboardInterrupt
    ):  # Ctrl-C : on restaure aussi, puis on laisse l'interruption se propager
        _rollback(paths, db, snap, data, "Interruption manuelle (Ctrl-C).", STOPPED)
        raise
    except Exception as exc:  # noqa: BLE001 - tout échec restaure l'état d'avant le cycle
        return _rollback(paths, db, snap, data, f"Erreur : {type(exc).__name__} : {exc}", FAILED)


def _finish(
    paths: Paths, auto: Autonomy, data: CycleData, run_id: str, db: Database
) -> CycleOutcome:
    db.close()
    text = render(data)
    report = write_report(auto.rapports_dir, data, text)
    ind = data.indicators
    append_log(paths.log, {
        "n": data.n, "cycle_id": data.cycle_id, "date": data.date.isoformat(), "status": OK, "run_id": run_id,
        "seuil_ingestion": data.ingestion.after if data.ingestion else None,
        "indicateurs": _ind_dict(ind), "rapport": str(report), "nouvelles_sources": len(data.collect.new_sources) if data.collect else 0,
        "mots_appris": sum(len(g.added) for g in data.learning),
    })  # fmt: skip
    return CycleOutcome(OK, data.cycle_id, data.n, run_id, report, text, ind, data)


def _ind_dict(ind: Indicators | None) -> dict:
    if ind is None:
        return {}
    return {
        "changement_de_grappe": round(ind.changed_share, 4), "taux_ambiguite": round(ind.ambiguity_rate, 4),
        "taux_confirmes": round(ind.confirmed_rate, 4), "chute_confirmes": round(ind.confirmed_drop, 4),
        "derive_lexique": None if ind.lexicon_drift == float("inf") else round(ind.lexicon_drift, 4),
        "depasses": ind.exceeded,
    }  # fmt: skip


def _rollback(
    paths: Paths, db: Database | None, snap: Snapshot, data: CycleData, reason: str, status: str
) -> CycleOutcome:
    """Restaure l'état d'avant le cycle (base, fichiers appris, sorties) et rend compte."""
    if db is not None:
        db.close()
    restore_snapshot(snap, paths.db, paths.config, paths.out)
    data.status, data.reason, data.rolled_back_to = status, reason, snap.cycle_id
    auto = load_autonomy(paths.config)
    text = render(data)
    report = write_report(auto.rapports_dir, data, text)
    append_log(paths.log, {
        "n": data.n, "cycle_id": data.cycle_id, "date": data.date.isoformat(), "status": status, "raison": reason,
        "indicateurs": _ind_dict(data.indicators), "rapport": str(report), "restaure": snap.cycle_id,
    })  # fmt: skip
    return CycleOutcome(status, data.cycle_id, data.n, None, report, text, data.indicators, data)


# --- retour en arrière manuel -------------------------------------------------------------------------


def rollback_last(
    config_dir: str | Path = "config", db_path: str | Path = "donnees/grappes.db",
    out_dir: str | Path = "sorties", data_dir: str | Path = "donnees",
) -> CycleOutcome:  # fmt: skip
    """`grappes rollback` : annule le dernier cycle terminé et non déjà annulé."""
    paths = Paths(Path(config_dir), Path(db_path), Path(out_dir), Path(data_dir))
    try:
        with cycle_lock(paths.lock):
            records = read_log(paths.log)
            undone = {r.get("annule") for r in records if r.get("status") == "rollback_manuel"}
            target = next(
                (
                    r
                    for r in reversed(records)
                    if r.get("status") == OK and r.get("n") not in undone
                ),
                None,
            )
            if target is None:
                return CycleOutcome(FAILED, extra={"reason": "aucun cycle à annuler"})
            snap = find_snapshot(paths.snapshots, target["cycle_id"])
            if snap is None:
                return CycleOutcome(
                    FAILED, extra={"reason": f"instantané introuvable pour {target['cycle_id']}"}
                )
            restore_snapshot(snap, paths.db, paths.config, paths.out)
            append_log(paths.log, {
                "n": next_cycle_number(paths.log), "date": datetime.now(UTC).isoformat(), "status": "rollback_manuel",
                "annule": target["n"], "cycle_id": target["cycle_id"], "restaure": snap.cycle_id,
            })  # fmt: skip
            return CycleOutcome(OK, target["cycle_id"], target["n"], extra={"annule": target["n"]})
    except CycleBusy as exc:
        return CycleOutcome(BUSY, extra={"reason": str(exc)})


# --- simulation --------------------------------------------------------------------------------------------


def _simulate(paths: Paths, auto: Autonomy, now: datetime, forced: bool) -> CycleOutcome:
    """Aucun appel, aucune écriture (ni base, ni journal, ni verrou, ni rapport sur disque)."""
    cfg = load_config(paths.config)
    scfg = load_search_config(paths.config)
    data = CycleData(next_cycle_number(paths.log), "simulation", now, "simulation (dry-run)", DRY)
    data.grappe_labels = _label_map(cfg)
    data.subjects = _subjects_info(cfg)
    data.include_persons = cfg.scoring.resultat_inclure_personnes
    if forced:
        data.reason = "`autonome` est faux dans config/autonomie.yaml : simulation seulement."
    plan = []
    if scfg.actifs:
        plan.append(f"fournisseurs de recherche actifs : {', '.join(scfg.actifs)}")
    else:
        plan.append(
            "aucun fournisseur de recherche actif (`actifs` vide dans config/recherche.yaml) : aucune collecte possible"
        )
    plan.append(f"limites par cycle : {auto.requetes_par_cycle} requêtes, {auto.pages_par_cycle} pages, {auto.nouvelles_sources_par_cycle} nouvelles sources, {auto.elements_par_cycle} observations, profondeur {auto.profondeur}")  # fmt: skip
    unseeded = [s for s in data.subjects if s[2] == 0]
    if unseeded:
        plan.append(f"{len(unseeded)} sujet(s) à amorcer d'abord (aucun mot de départ)")
    data.alerts = ["Si `autonome` était vrai, ce cycle ferait : " + " ; ".join(plan) + "."]
    if not data.subjects:
        data.alerts.append("Aucun sujet dans config/sujets.yaml : rien à suivre.")
    if paths.db.exists():  # lecture seule : jamais de migration ni de création
        conn = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT run_id FROM runs ORDER BY ts DESC, rowid DESC LIMIT 1"
            ).fetchone()
            holder = type("RO", (), {"conn": conn})()
            data.after = run_metrics(holder, row["run_id"] if row else None, cfg)
        except sqlite3.Error:
            data.after = None
        finally:
            conn.close()
    text = render(data)
    return CycleOutcome(DRY, "simulation", data.n, None, None, text, None, data)
