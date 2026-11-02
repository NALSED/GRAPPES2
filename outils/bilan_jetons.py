"""Bilan des économies de jetons -> ECONOMIES.md (régénérable : `.venv/bin/python outils/bilan_jetons.py`).

Sources, et rien d'autre :
* `donnees/.journal_ccr.log` : un appel routé vers le compte gratuit par ligne (« date | description »).
  Le journal ne consigne ni jetons ni octets : le volume est affiché comme absent, jamais estimé.
* `rtk gain --daily --format json` (hook RTK) : gain estimé (octets / 4) sur la seule sortie interceptée
  par rtk. Lancé en lecture seule, jamais `--reset`.

Règles : aucune valeur inventée ; une donnée absente est écrite « non disponible » ; une source vide ou
illisible est signalée et le rapport continue. Les ratios octets/jeton de la marge d'incertitude sont des
HYPOTHÈSES déclarées (options `--ratio-bas`, `--ratio-haut`), pas des mesures.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

ABSENT = "non disponible"
RTK_BYTES_PER_TOKEN = 4  # convention de rtk : jetons estimés = octets / 4 (MISSION § 4.6)


# --- journal du compte gratuit ------------------------------------------------------------------


@dataclass(slots=True)
class Journal:
    entries: list[tuple[datetime, str]] = field(default_factory=list)
    bad_lines: list[tuple[int, str]] = field(default_factory=list)  # (n° de ligne, contenu)
    error: str | None = None  # fichier absent ou illisible

    @property
    def per_day(self) -> Counter:
        return Counter(ts.date() for ts, _ in self.entries)


def parse_journal(text: str) -> Journal:
    """`2026-10-06T11:16:29+04:00 | description` par ligne ; les autres lignes sont signalées."""
    j = Journal()
    for n, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        stamp, sep, desc = raw.partition("|")
        try:
            if not sep:
                raise ValueError("séparateur « | » manquant")
            j.entries.append((datetime.fromisoformat(stamp.strip()), desc.strip()))
        except ValueError:
            j.bad_lines.append((n, raw.strip()[:120]))
    return j


def load_journal(path: Path) -> Journal:
    try:
        return parse_journal(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        j = Journal()
        j.error = f"{type(exc).__name__}: {exc}"
        return j


# --- traces RTK -----------------------------------------------------------------------------------


@dataclass(slots=True)
class Rtk:
    commands: int = 0
    saved: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    daily: dict[date, dict] = field(default_factory=dict)
    error: str | None = None  # source absente, vide ou illisible

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_rtk(payload: str) -> Rtk:
    """Sortie JSON de `rtk gain --daily --format json`."""
    r = Rtk()
    try:
        data = json.loads(payload)
        summary = data["summary"]
        r.commands = int(summary["total_commands"])
        r.saved = int(summary["total_saved"])
        r.input_tokens = int(summary.get("total_input", 0))
        r.output_tokens = int(summary.get("total_output", 0))
        for d in data.get("daily", []):
            r.daily[date.fromisoformat(d["date"])] = {
                "commands": int(d["commands"]),
                "saved": int(d["saved_tokens"]),
                "input": int(d.get("input_tokens", 0)),
                "pct": float(d["savings_pct"]) if d.get("savings_pct") is not None else None,
            }
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return Rtk(error=f"JSON RTK illisible ({type(exc).__name__}: {exc})")
    if r.commands == 0 and not r.daily:
        r.error = "RTK n'a enregistré aucune commande"
    return r


def run_rtk(project: bool, cwd: Path | None = None) -> Rtk:
    cmd = ["rtk", "gain", "--daily", "--format", "json"] + (["--project"] if project else [])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Rtk(error=f"rtk indisponible ({type(exc).__name__})")
    if out.returncode != 0:
        return Rtk(error=f"rtk a échoué (code {out.returncode})")
    return parse_rtk(out.stdout)


# --- rapport --------------------------------------------------------------------------------------


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def build_report(
    journal: Journal,
    rtk_project: Rtk,
    rtk_account: Rtk,
    ratio_low: float = 3.0,
    ratio_high: float = 5.0,
    generated: datetime | None = None,
    journal_path: str = "donnees/.journal_ccr.log",
) -> str:
    generated = generated or datetime.now().astimezone()
    L: list[str] = []
    w = L.append
    w("# ECONOMIES — bilan des économies de jetons\n")
    w(f"Généré le {generated.strftime('%Y-%m-%d %H:%M')} par `outils/bilan_jetons.py` "
      "(régénérable : `.venv/bin/python outils/bilan_jetons.py`). Aucune valeur n'est inventée : "
      f"une donnée absente est écrite « {ABSENT} ».\n")  # fmt: skip

    # 1. compte gratuit
    w("## 1. Appels routés vers le compte gratuit\n")
    per_day = journal.per_day
    if journal.error:
        w(f"Source illisible : `{journal_path}` ({journal.error}). Aucun chiffre.\n")
        first = last = None
    elif not journal.entries:
        w(f"Le journal `{journal_path}` est vide : **0 appel** enregistré.\n")
        first = last = None
    else:
        first, last = min(per_day), max(per_day)
        w(f"* Appels enregistrés : **{len(journal.entries)}**")
        w(f"* Période couverte : du {first.isoformat()} au {last.isoformat()} "
          f"({(last - first).days + 1} jour(s), dont {len(per_day)} avec au moins un appel)")  # fmt: skip
        w(f"* Volume estimé en jetons : **{ABSENT}** (le journal ne consigne que l'horodatage et la "
          "description : ni jetons, ni octets ; aucune estimation n'est faite à la place)\n")  # fmt: skip
    if journal.bad_lines:
        w(f"Lignes illisibles ignorées : **{len(journal.bad_lines)}**.")
        for n, content in journal.bad_lines[:10]:
            w(f"* ligne {n} : `{content}`")
        w("")

    # 2. RTK
    w("## 2. Gain du hook RTK (ESTIMATION, pas une mesure)\n")
    w("`rtk gain` donne un gain **estimé** (octets / 4), uniquement sur la sortie que rtk a interceptée "
      "(commandes shell et lectures de fichiers). Ce n'est **pas** l'économie totale de jetons ni de "
      "coût de la session : il ne couvre ni les réponses du modèle, ni les outils non interceptés, ni "
      "le routage vers le compte gratuit.\n")  # fmt: skip
    for title, r in (
        ("Ce projet (`rtk gain --project`)", rtk_project),
        ("Compte entier, tous projets", rtk_account),
    ):
        if not r.ok:
            w(f"* {title} : **{ABSENT}** — {r.error}")
            continue
        pct = f"{100 * r.saved / r.input_tokens:.1f} %" if r.input_tokens else ABSENT
        w(f"* {title} : {_fmt(r.commands)} commandes, {_fmt(r.input_tokens)} jetons estimés en entrée, "
          f"**{_fmt(r.saved)} jetons estimés économisés** ({pct} de l'entrée interceptée)")  # fmt: skip
    w("")

    # 3. consolidé
    w("## 3. Total consolidé\n")
    if rtk_project.ok:
        low = rtk_project.saved * RTK_BYTES_PER_TOKEN / ratio_high
        high = rtk_project.saved * RTK_BYTES_PER_TOKEN / ratio_low
        w(f"* **Total consolidé : {_fmt(rtk_project.saved)} jetons estimés économisés** (gain RTK du projet) "
          "+ appels gratuits non chiffrables en jetons (voir § 1).")  # fmt: skip
        w(f"* Marge d'incertitude, **sous hypothèse** de {ratio_low:g} à {ratio_high:g} octets par jeton "
          f"(RTK suppose {RTK_BYTES_PER_TOKEN}) : de {_fmt(round(low))} à {_fmt(round(high))} jetons. "
          "Ces deux ratios sont des hypothèses modifiables, pas des mesures.")  # fmt: skip
        w("* Le compte gratuit n'ajoute aucun montant : le nombre d'appels est connu, pas leur taille. "
          "La somme est donc un **minimum chiffrable**, pas un total.\n")  # fmt: skip
    else:
        w(f"* Total consolidé : **{ABSENT}** (source RTK du projet absente : {rtk_project.error}).")
        w("* Le compte gratuit n'est pas chiffrable en jetons (voir § 1).\n")

    # 4. par jour
    w("## 4. Une ligne par jour\n")
    days: set[date] = set(per_day) | set(rtk_project.daily)
    if first and last:
        days |= set(_days(first, last))
    if not days:
        w("Aucune donnée journalière dans les sources disponibles.\n")
    else:
        w(
            "| Jour | Appels gratuits | Commandes RTK (projet) | Jetons économisés RTK (projet, estimés) | Gain RTK |"
        )
        w("| --- | --- | --- | --- | --- |")
        for d in sorted(days):
            if journal.error or not journal.entries:
                free = ABSENT
            elif first <= d <= last:
                free = str(
                    per_day.get(d, 0)
                )  # dans la période du journal, pas de ligne = pas d'appel
            else:
                free = ABSENT  # hors période couverte par le journal
            r = rtk_project.daily.get(d) if rtk_project.ok else None
            cmds = str(r["commands"]) if r else ABSENT
            saved = _fmt(r["saved"]) if r else ABSENT
            pct = f"{r['pct']:.1f} %" if r and r["pct"] is not None else ABSENT
            w(f"| {d.isoformat()} | {free} | {cmds} | {saved} | {pct} |")
        w("")

    # 5. hypothèses
    w("## 5. Hypothèses de calcul\n")
    w("* **Journal** : une ligne = un appel routé ; une ligne illisible n'est pas comptée. Dans la période "
      "[première ligne, dernière ligne], un jour sans ligne vaut 0 appel ; hors de cette période, la "
      "donnée est absente. Le jour est celui de l'horodatage écrit dans la ligne (son propre fuseau).")  # fmt: skip
    w(f"* **RTK** : les jetons sont ceux que rtk estime (octets / {RTK_BYTES_PER_TOKEN}). Le périmètre "
      "« projet » est celui du dossier courant au lancement ; le périmètre « compte » couvre tous les "
      "projets. Aucune valeur RTK n'est recalculée ni complétée.")  # fmt: skip
    w(f"* **Marge** : jetons = octets / ratio ; octets = jetons RTK × {RTK_BYTES_PER_TOKEN} ; ratio bas "
      f"{ratio_low:g}, ratio haut {ratio_high:g} (hypothèses de l'auteur du script, à ajuster).")  # fmt: skip
    w("* **Non couvert** : coût en jetons des appels du compte gratuit, économies hors RTK, qualité "
      "comparée des réponses. Aucune de ces valeurs n'est estimée ici.")  # fmt: skip
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description="Bilan des économies de jetons -> ECONOMIES.md")
    p.add_argument("--journal", type=Path, default=root / "donnees" / ".journal_ccr.log")
    p.add_argument("--out", type=Path, default=root / "ECONOMIES.md")
    p.add_argument(
        "--rtk-json",
        type=Path,
        help="JSON `rtk gain --daily --format json` déjà enregistré (projet)",
    )
    p.add_argument("--rtk-json-compte", type=Path, help="idem pour le compte entier")
    p.add_argument(
        "--ratio-bas", type=float, default=3.0, help="hypothèse : octets par jeton (borne basse)"
    )
    p.add_argument(
        "--ratio-haut", type=float, default=5.0, help="hypothèse : octets par jeton (borne haute)"
    )
    a = p.parse_args(argv)
    if not 0 < a.ratio_bas <= a.ratio_haut:
        print("--ratio-bas doit vérifier 0 < bas <= haut", file=sys.stderr)
        return 2

    def rtk(path: Path | None, project: bool) -> Rtk:
        if path is None:
            return run_rtk(project, cwd=root)
        try:
            return parse_rtk(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            return Rtk(error=f"fichier RTK illisible ({type(exc).__name__})")

    report = build_report(
        load_journal(a.journal),
        rtk(a.rtk_json, True),
        rtk(a.rtk_json_compte, False),
        a.ratio_bas,
        a.ratio_haut,
        journal_path=str(a.journal.relative_to(root))
        if a.journal.is_relative_to(root)
        else str(a.journal),
    )
    a.out.write_text(report, encoding="utf-8")
    print(f"{a.out} écrit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
