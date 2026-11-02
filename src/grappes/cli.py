"""CLI minimale : ingest, run, export."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from grappes.config import ConfigError, load_config
from grappes.ingestion import registered_ecosystems
from grappes.pipeline import export, ingest, run
from grappes.storage.db import Database


def _explain(db: Database, cfg, entity_id: str, grappe_id: str | None) -> int:
    from grappes.mcp.queries import Queries

    q = Queries(db.conn, cfg)
    try:
        fiche = q.get_entity(entity_id)
        if grappe_id is None:
            if not fiche["memberships"]:
                print(f"{entity_id} : aucune appartenance (non classé)")
                return 0
            grappe_id = fiche["memberships"][0]["grappe_id"]
        out = {"entite": fiche, "explication": q.explain_assignment(entity_id, grappe_id)}
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def _search(args, cfg) -> int:
    from grappes.autonomy import load_autonomy
    from grappes.search.config import load_search_config
    from grappes.search.engine import run_search, split_keywords, write_candidates_csv
    from grappes.search.internal import internal_candidates

    try:
        scfg = load_search_config(args.config)
    except ConfigError as exc:
        print(f"configuration de recherche invalide : {exc}", file=sys.stderr)
        return 2
    keywords = split_keywords(args.keywords or "")
    if args.sujet:
        keywords = [args.sujet.strip(), *[k for k in keywords if k != args.sujet.strip()]]
    if args.grappe:
        grappe = next((g for g in cfg.grappes if g.grappe_id == args.grappe), None)
        if grappe is None:
            print(f"grappe inconnue : {args.grappe}", file=sys.stderr)
            return 2
        keywords += [t for t in grappe.lexicon.terms if t not in keywords]
    internal = []
    if not args.no_internal and Path(args.db).exists():
        db = Database(args.db)
        try:
            internal = internal_candidates(db)
        finally:
            db.close()
    if not keywords and not internal:
        print("aucun mot-clé (--keywords ou --grappe) et aucun lien déjà cité", file=sys.stderr)
        return 2
    try:
        autonomy = load_autonomy(args.config)
    except ConfigError as exc:
        print(f"configuration d'autonomie invalide : {exc}", file=sys.stderr)
        return 2
    if autonomy.stop_file.exists():
        print(f"arrêt demandé : {autonomy.stop_file} existe, aucune recherche", file=sys.stderr)
        return 4
    rep = run_search(
        scfg,
        keywords,
        providers=args.provider,
        execute=autonomy.autonome,
        extra=internal,
        sujet=(args.sujet or "").strip(),
    )
    for pid, status in rep.status.items():
        print(f"{pid} : {status}")
    merged = rep.candidates
    path = write_candidates_csv(Path(args.out) / "candidats.csv", merged, cfg.scoring.csv_separator)
    mode = "appels réels" if rep.executed else "dry-run : aucun appel externe"
    print(f"{len(merged)} candidats ({mode}, {rep.requests} requêtes) -> {path}")
    if not rep.executed:
        print("`autonome` est faux (config/autonomie.yaml) : dry-run, aucun appel réel.")
    return 0


def _cycle(args) -> int:
    from grappes.cycle.engine import BUSY, DRY, FAILED, OK, STOPPED, rollback_last, run_cycle

    try:
        if args.cmd == "rollback":
            out = rollback_last(args.config, args.db, args.out, args.data)
            if out.status == OK:
                print(f"cycle {out.n} annulé : état d'avant restauré")
                return 0
        else:
            out = run_cycle(args.config, args.db, args.out, args.data, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"configuration invalide : {exc}", file=sys.stderr)
        return 2
    reason = out.extra.get("reason", "")
    if out.status == BUSY or out.status == STOPPED and not out.report:
        print(reason, file=sys.stderr)
    elif out.status == FAILED and not out.report:
        print(reason or "échec", file=sys.stderr)
    else:
        print(out.report, end="")
        if out.report_path:
            print(f"\nrapport : {out.report_path}")
        if out.status == DRY:
            print("simulation : rien n'a été appelé ni écrit.")
    return out.code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="grappes")
    p.add_argument("--db", default="donnees/grappes.db")
    p.add_argument("--config", default="config")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest")
    s.add_argument("folder")
    s.add_argument("--ecosystem", required=True, choices=registered_ecosystems())
    sub.add_parser("run")
    e = sub.add_parser("export")
    e.add_argument("--out", default="sorties")
    x = sub.add_parser("explain")
    x.add_argument("entity_id")
    x.add_argument("--grappe", help="grappe à expliquer (défaut : grappe principale)")
    pg = sub.add_parser("purge")
    pg.add_argument(
        "--older-than",
        type=int,
        metavar="JOURS",
        help="défaut : retention_days de scoring.yaml (90)",
    )
    sub.add_parser("mcp")
    se = sub.add_parser(
        "search", help="recherche de candidats (dry-run tant que `autonome` est faux)"
    )
    se.add_argument("--keywords", help="mots-clés séparés par des virgules")
    se.add_argument("--grappe", help="identifiant d'une grappe : ses termes servent de mots-clés")
    se.add_argument("--provider", action="append", help="fournisseur (répétable ; défaut : actifs)")
    se.add_argument("--sujet", help="phrase du sujet : gabarit {sujet} et mot-clé de départ")
    se.add_argument("--no-internal", action="store_true", help="sans les liens déjà cités")
    se.add_argument("--out", default="sorties")
    cy = sub.add_parser(
        "cycle", help="boucle automatique (simulation tant que `autonome` est faux)"
    )
    cy.add_argument(
        "--dry-run", action="store_true", help="simulation : rien n'est appelé ni écrit"
    )
    cy.add_argument("--out", default="sorties")
    cy.add_argument(
        "--data", default="donnees", help="dossier du verrou, du journal et des instantanés"
    )
    rb = sub.add_parser("rollback", help="annule le dernier cycle terminé")
    rb.add_argument("--out", default="sorties")
    rb.add_argument("--data", default="donnees")
    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration invalide : {exc}", file=sys.stderr)
        return 2
    if args.cmd in (
        "search",
        "cycle",
    ):  # secrets (clés des fournisseurs) : config/secrets.env, hors dépôt
        from grappes.env import load_secrets

        load_secrets(Path(args.config) / "secrets.env")
    if args.cmd == "search":
        return _search(args, cfg)
    if args.cmd in ("cycle", "rollback"):
        return _cycle(args)
    if args.cmd == "mcp":
        from grappes.mcp.server import create_server

        try:
            create_server(args.db, args.config).run()
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return 2
        return 0
    db = Database(args.db)
    try:
        if args.cmd == "ingest":
            if not Path(args.folder).is_dir():
                print(f"dossier introuvable : {args.folder}", file=sys.stderr)
                return 2
            new, rep = ingest(db, args.folder, args.ecosystem, cfg)
            print(
                f"{new} nouvelles observations, {rep.duplicates} doublons, {rep.n_rejected} rejets"
                + (f", {rep.ignored} ignorés" if rep.ignored else "")
            )
            if rep.n_rejected and not (new or rep.duplicates):
                print(
                    f"tous les enregistrements ont été rejetés : mauvais --ecosystem "
                    f"({args.ecosystem}) ou format inattendu ?",
                    file=sys.stderr,
                )
                return 1
        elif args.cmd == "run":
            run_id, res = run(db, cfg)
            print(f"{run_id} : {len(res.entities)} entités, {len(res.memberships)} appartenances")
        elif args.cmd == "export":
            for path in export(db, cfg, args.out):
                print(path)
        elif args.cmd == "purge":
            days = cfg.scoring.retention_days if args.older_than is None else args.older_than
            if days is None:
                print("--older-than est requis : retention_days est illimité", file=sys.stderr)
                return 2
            if days < 0:
                print("--older-than doit être positif ou nul", file=sys.stderr)
                return 2
            print(f"{db.purge_older_than(days)} observations supprimées")
        elif args.cmd == "explain":
            return _explain(db, cfg, args.entity_id, args.grappe)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
