"""État d'une boucle : verrou anti-concurrence, journal des cycles, instantanés versionnés.

Un instantané capture l'état AVANT un cycle (base SQLite, fichiers appris, sorties). Restaurer
un instantané annule donc exactement ce cycle. Les fichiers écrits par Sednal (`config/grappes/`,
`config/sujets.yaml`, `config/autonomie.yaml`...) ne sont jamais capturés ni restaurés : ce n'est pas
l'état de la boucle et ils ne sont jamais modifiés par elle.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from grappes.learned import learned_dir


class CycleBusy(RuntimeError):
    """Un autre cycle est déjà en cours."""


@contextmanager
def cycle_lock(path: str | Path) -> Iterator[None]:
    """Verrou exclusif non bloquant (`flock`) : libéré par le système si le processus meurt."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CycleBusy(f"un cycle est déjà en cours ({p})") from None
        try:
            f.seek(0)
            f.truncate()
            f.write(f"{datetime.now(UTC).isoformat()}\n")
            f.flush()
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# --- journal des cycles -------------------------------------------------------------------------


def read_log(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # ligne abîmée : ignorée, jamais fatale
    return out


def append_log(path: str | Path, record: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def next_cycle_number(log_path: str | Path) -> int:
    return max((int(r.get("n", 0)) for r in read_log(log_path)), default=0) + 1


# --- instantanés ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Snapshot:
    cycle_id: str
    path: Path


def _copy_dir(src: Path, dst: Path) -> bool:
    if src.is_dir():
        shutil.copytree(src, dst)
        return True
    return False


def take_snapshot(
    root: str | Path,
    cycle_id: str,
    db_path: str | Path,
    config_dir: str | Path,
    out_dir: str | Path,
) -> Snapshot:
    """Copie la base (API de sauvegarde SQLite, cohérente), les fichiers appris et les sorties."""
    snap = Path(root) / cycle_id
    snap.mkdir(parents=True, exist_ok=True)
    meta = {
        "cycle_id": cycle_id,
        "date": datetime.now(UTC).isoformat(),
        "db": False,
        "appris": False,
        "sorties": False,
    }
    if Path(db_path).exists():
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(snap / "grappes.db"))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        meta["db"] = True
    meta["appris"] = _copy_dir(learned_dir(config_dir), snap / "apprentissage")
    meta["sorties"] = _copy_dir(Path(out_dir), snap / "sorties")
    (snap / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return Snapshot(cycle_id, snap)


def restore_snapshot(
    snap: Snapshot, db_path: str | Path, config_dir: str | Path, out_dir: str | Path
) -> None:
    """Remet l'état d'avant le cycle. La connexion à la base doit être fermée par l'appelant."""
    meta = json.loads((snap.path / "meta.json").read_text(encoding="utf-8"))
    db = Path(db_path)
    for sidecar in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        sidecar.unlink(missing_ok=True)
    if meta["db"]:
        db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(snap.path / "grappes.db", db)
    else:
        db.unlink(missing_ok=True)  # il n'y avait pas de base avant le cycle
    for key, live, saved in (
        ("appris", learned_dir(config_dir), snap.path / "apprentissage"),
        ("sorties", Path(out_dir), snap.path / "sorties"),
    ):
        shutil.rmtree(live, ignore_errors=True)
        if meta[key]:
            shutil.copytree(saved, live)


def prune_snapshots(root: str | Path, keep: int) -> list[str]:
    """Garde les `keep` instantanés les plus récents (ordre des identifiants) ; retourne les supprimés."""
    base = Path(root)
    if not base.is_dir():
        return []
    dirs = sorted(p for p in base.iterdir() if p.is_dir() and (p / "meta.json").exists())
    removed = []
    for p in dirs[: max(len(dirs) - keep, 0)]:
        shutil.rmtree(p, ignore_errors=True)
        removed.append(p.name)
    return removed


def find_snapshot(root: str | Path, cycle_id: str) -> Snapshot | None:
    p = Path(root) / cycle_id
    return Snapshot(cycle_id, p) if (p / "meta.json").exists() else None
