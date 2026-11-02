"""Export CSV : entites.csv (une ligne par entité) et appartenances.csv (format long)."""

from __future__ import annotations

import csv
from pathlib import Path

from grappes.clustering.assign import NON_CLASSE, SECONDAIRE, AssignResult, level
from grappes.config import Config
from grappes.models import EntityType
from grappes.redact import pseudonym

ENTITY_COLUMNS = [
    "entity_id",
    "entity_type",
    "display_name",
    "primary_ref",
    "ecosystems",
    "cluster_id",
    "confidence_score",
    "confidence_level",
    "status",
    "semantic_score",
    "ecosystem_score",
    "anchor_score",
    "validated_via_crossref",
    "n_observations",
    "n_citing_sources",
    "first_seen",
    "last_seen",
    "run_id",
    "admiralty",
]
MEMBERSHIP_COLUMNS = ["entity_id", "cluster_id", "confidence_score", "via", "status"]


def _fmt(ts) -> str:
    return ts.isoformat() if ts else ""


def safe_cell(value: str) -> str:
    """Neutralise l'injection de formule tableur : une cellule texte ne commence pas par = + - @."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _open(path: Path, cfg: Config):
    return path.open("w", encoding="utf-8-sig" if cfg.scoring.csv_bom else "utf-8", newline="")


def export_csv(result: AssignResult, cfg: Config, run_id: str, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    by_entity: dict[str, list[dict]] = {}
    for m in result.memberships:
        by_entity.setdefault(m["entity_id"], []).append(m)

    entites, appart = out / "entites.csv", out / "appartenances.csv"
    sep = cfg.scoring.csv_separator
    with _open(entites, cfg) as f:
        w = csv.writer(f, delimiter=sep, lineterminator="\n")
        w.writerow(ENTITY_COLUMNS)
        for e in sorted(result.entities, key=lambda e: e.entity_id):
            st = result.stats[e.entity_id]
            ms = sorted(
                by_entity.get(e.entity_id, []), key=lambda m: (-m["confidence"], m["grappe_id"])
            )
            main = ms[0] if ms else None
            hidden = cfg.scoring.redact_persons and e.type is EntityType.PERSON
            classed = main is not None and main["status"] != NON_CLASSE
            w.writerow(
                [
                    e.entity_id,
                    e.type.value,
                    "" if hidden else safe_cell(e.display_name or ""),
                    pseudonym(e.primary_ref) if hidden else safe_cell(e.primary_ref),
                    ",".join(sorted(st.ecosystems)),
                    main["grappe_id"] if classed else "",
                    f"{main['confidence']:.2f}" if main else "0.00",
                    level(main["confidence"], cfg, main["via"] == "recoupement")
                    if main
                    else "faible",
                    main["status"] if main else NON_CLASSE,
                    f"{main['semantic']:.2f}" if main else "0.00",
                    f"{main['ecosystem']:.2f}" if main else "0.00",
                    f"{main['anchor']:.2f}" if main else "0.00",
                    "oui" if main and classed and main["via"] == "recoupement" else "non",
                    st.n_observations,
                    len(st.citing),
                    _fmt(st.first_seen),
                    _fmt(st.last_seen),
                    run_id,
                    result.admiralty.get(
                        e.primary_ref, ""
                    ),  # fiabilité de la source + crédibilité (ex. B2)
                ]
            )
    with _open(appart, cfg) as f:
        w = csv.writer(f, delimiter=sep, lineterminator="\n")
        w.writerow(MEMBERSHIP_COLUMNS)
        for m in sorted(
            result.memberships, key=lambda m: (m["entity_id"], -m["confidence"], m["grappe_id"])
        ):
            if m["status"] == NON_CLASSE:
                continue
            w.writerow(
                [m["entity_id"], m["grappe_id"], f"{m['confidence']:.2f}", m["via"], m["status"]]
            )
    return [entites, appart]


__all__ = ["SECONDAIRE", "export_csv"]
