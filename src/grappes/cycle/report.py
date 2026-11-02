"""Rapport de cycle `rapports/cycle_<date>.md`, lisible par un non-informaticien.

Phrases courtes, aucun identifiant technique inutile, aucun nom de personne physique (masquage
`redact_persons`), motifs de refus en clair. Une section « Alertes » dit ce qui demande un regard."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from grappes.cycle.collect import CollectReport
from grappes.cycle.learn import GrappeLearning, IngestionAdjust, Recalibration
from grappes.cycle.metrics import LABELS, Indicators, Metrics

MAX_LIST = 15


@dataclass(slots=True)
class CycleData:
    n: int
    cycle_id: str
    date: datetime
    mode: str  # « réel » ou « simulation (dry-run) »
    status: str  # ok | annule | arret | erreur | dry-run
    reason: str = ""
    subjects: list[tuple[str, str, int]] = field(default_factory=list)  # (id, phrase, nb de termes)
    seeded: list[str] = field(default_factory=list)  # sujets amorcés pendant ce cycle
    collect: CollectReport | None = None
    purged: int = 0
    fiabilite: dict[str, object] = field(default_factory=dict)  # registres, Tranco, lettres
    before: Metrics | None = None
    after: Metrics | None = None
    indicators: Indicators | None = None
    changed: list[tuple[str, str, str]] = field(default_factory=list)  # (nom, grappe avant, après)
    learning: list[GrappeLearning] = field(default_factory=list)
    recalibration: Recalibration | None = None
    ingestion: IngestionAdjust | None = None
    hypothetical_citations: int = 0
    hypothetical_sources: int = 0
    include_persons: bool = False
    alerts: list[str] = field(default_factory=list)
    rolled_back_to: str = ""
    grappe_labels: dict[str, str] = field(default_factory=dict)


def _g(data: CycleData, gid: str) -> str:
    return data.grappe_labels.get(gid, gid)


def render(d: CycleData) -> str:
    L: list[str] = []
    w = L.append
    state = {
        "ok": "Cycle terminé normalement.",
        "annule": "CYCLE ANNULÉ : l'état précédent a été restauré.",
        "arret": "CYCLE INTERROMPU sur demande d'arrêt : l'état précédent a été restauré.",
        "erreur": "CYCLE EN ERREUR : l'état précédent a été restauré.",
        "dry-run": "Simulation : rien n'a été appelé ni écrit.",
    }[d.status]
    w(f"# Rapport du cycle {d.n} — {d.date.strftime('%d/%m/%Y %H:%M')}\n")
    w(f"**Mode : {d.mode}.** {state}")
    if d.reason:
        w(f"\n> {d.reason}")
    w("")

    # --- alertes d'abord : ce qui demande un regard
    alerts = list(d.alerts)
    if d.include_persons:
        alerts.append(
            "ATTENTION : `inclure_personnes` est vrai dans `config/scoring.yaml`. Les noms de personnes "
            "physiques figurent EN CLAIR dans `resultat.csv` ; le masquage ne s'applique plus à ce fichier."
        )
    if d.collect and d.collect.blocked_domains:
        alerts.append("Sites qui ont refusé les visites (403/429 ou contrôle anti-robot), arrêt sur ces sites : "
                      + ", ".join(sorted(set(d.collect.blocked_domains))[:MAX_LIST]))  # fmt: skip
    if d.collect and d.collect.stopped:
        alerts.append("La collecte a été arrêtée avant la fin (fichier STOP).")
    if d.collect:
        for pid, st in d.collect.providers.items():
            if not st.startswith(("ok", "dry-run")):
                alerts.append(f"Fournisseur de recherche « {pid} » : {st}")
    w("## Alertes\n")
    if alerts:
        for a in alerts:
            w(f"* {a}")
    else:
        w("Aucune alerte.")
    w("")

    # --- sujets
    w("## Sujets suivis\n")
    if not d.subjects:
        w("Aucun sujet défini (`config/sujets.yaml`).")
    for sid, phrase, n_terms in d.subjects:
        tag = " (amorcé pendant ce cycle)" if sid in d.seeded else ""
        w(f"* « {phrase} » — {n_terms} mot(s) de départ{tag}")
        if n_terms == 0:
            w(
                "  * Pas encore de mots de départ : l'amorçage n'a trouvé aucun résultat exploitable."
            )
    w("")

    # --- ce qui est entré
    w("## Ce qui est entré\n")
    c = d.collect
    if c is None or (c.dry_run and not c.ingested):
        w(
            "Rien : aucune collecte n'a été lancée."
            if d.mode.startswith("simulation")
            else "Aucune collecte."
        )
    else:
        w(f"* Requêtes de recherche : {c.requests}")
        w(f"* Résultats examinés : {c.candidates}")
        w(f"* Pages visitées : {c.pages_attempted}")
        w(f"* Nouvelles sources : **{len(c.new_sources)}**")
        for dom in c.new_sources[:MAX_LIST]:
            w(f"  * {dom}")
        if len(c.new_sources) > MAX_LIST:
            w(f"  * … et {len(c.new_sources) - MAX_LIST} autre(s)")
        w(f"* Observations ajoutées : **{c.observations_new}**")
        if c.refused:
            w("* Refusé, et pourquoi :")
            for reason, n in Counter(c.refused).most_common(MAX_LIST):
                w(f"  * {n} × {reason}")
    if d.fiabilite:
        f = d.fiabilite
        w(f"* Fiabilité des sources (code Admiralty) : {f.get('lettres', '-')}")
        w(
            f"* Registres officiels : {f.get('requetes_registres', 0)} requête(s) ce cycle, "
            f"{f.get('en_a', 0)} source(s) placée(s) en A"
        )
        w(f"* Classement Tranco : {f.get('tranco', 'non utilisé')}")
    if d.purged:
        w(f"* Observations supprimées par la durée de conservation : {d.purged}")
    w("")

    # --- état
    w("## Où en sont les entités\n")
    if d.after:
        a, b = d.after, d.before
        w(f"* Entités connues : {a.n_entities}")
        w(f"* Rangées dans une grappe : {a.n_classed} (dont {a.n_ambiguous} ambiguës)")
        w(f"* Confirmées (recoupées sur au moins deux écosystèmes) : {a.n_confirmed}")
        if b and b.run_id:
            w(f"* Avant ce cycle : {b.n_classed} rangées, {b.n_confirmed} confirmées")
        w("* Une source qui n'est pas recoupée reste « probable » au mieux.")
    else:
        w("Pas de résultat pour ce cycle.")
    w("")
    w("## Ce qui a changé de grappe\n")
    if d.changed:
        for name, g0, g1 in d.changed[:MAX_LIST]:
            w(f"* {name} : {g0} → {g1}")
        if len(d.changed) > MAX_LIST:
            w(f"* … et {len(d.changed) - MAX_LIST} autre(s)")
    else:
        w("Aucune entité n'a changé de grappe.")
    w("")

    # --- apprentissage
    w("## Mots appris, seuils\n")
    if not d.learning:
        w("Pas d'apprentissage dans ce cycle.")
    for gl in d.learning:
        w(f"**{_g(d, gl.grappe_id)}** : {gl.n_teachers} entité(s) confirmée(s) fiable(s) sur {gl.n_confirmed}"
          + (f" ({gl.n_untrusted} écartée(s) faute de lien avec une source de confiance)" if gl.n_untrusted else ""))  # fmt: skip
        if gl.note:
            w(f"* {gl.note}")
        if gl.added:
            w("* Mots appris : " + ", ".join(sorted(gl.added)))
        if gl.expired:
            w("* Mots oubliés (non revus) : " + ", ".join(sorted(gl.expired)))
        if gl.kept:
            w(f"* Mots appris déjà connus et confirmés : {len(gl.kept)}")
    rc = d.recalibration
    if rc and rc.changed:
        for k, label in (
            ("seuil_min", "seuil de classement"),
            ("marge_ambiguite", "marge d'ambiguïté"),
        ):
            if abs(rc.after[k] - rc.before[k]) > 1e-9:
                w(f"* Seuil modifié — {label} : {rc.before[k]:g} → {rc.after[k]:g}")
    elif rc:
        w(f"* Seuils inchangés{(' (' + rc.note + ')') if rc.note else ''}.")
    ig = d.ingestion
    if ig is not None:
        if ig.changed:
            w(
                f"* **Seuil d'ingestion modifié** : {ig.before:g} → {ig.after:g}. Raison : {ig.reason}."
            )
        else:
            w(f"* Seuil d'ingestion inchangé ({ig.before:g}) : {ig.reason}.")
    w("")

    # --- Telegram
    if d.hypothetical_citations or d.hypothetical_sources:
        w("## Identités déduites d'un nom\n")
        w(f"{d.hypothetical_sources} canal(aux) Telegram n'ont qu'une identité déduite de leur nom affiché ; "
          f"{d.hypothetical_citations} citation(s) en dépendent. Elles sont conservées mais ne valident "
          "jamais un recoupement. Pour les fiabiliser, renseigner `config/telegram.yaml`.\n")  # fmt: skip

    # --- stabilité
    w("## Stabilité\n")
    ind = d.indicators
    if ind is None:
        w("Non calculée.")
    else:
        w(f"* Entités qui changent de grappe : {ind.changed_share:.0%}")
        w(f"* Part d'entités ambiguës : {ind.ambiguity_rate:.0%}")
        w(
            f"* Part d'entités confirmées : {ind.confirmed_rate:.0%} (baisse : {ind.confirmed_drop:.0%})"
        )
        drift = "infinie" if ind.lexicon_drift == float("inf") else f"{ind.lexicon_drift:.0%}"
        w(f"* Dérive du lexique appris : {drift}")
        if ind.exceeded:
            w("\n**Seuils dépassés : " + " ; ".join(LABELS.get(e, e) for e in ind.exceeded) + ".**")
    if d.rolled_back_to:
        w(f"\n**Retour en arrière** vers l'instantané `{d.rolled_back_to}`. Rien de ce cycle n'est conservé "
          "(base, mots appris, seuils, sorties).")  # fmt: skip
    w("")
    w("Commandes utiles : `grappes rollback` (annuler le dernier cycle), fichier `donnees/STOP` (arrêt immédiat), "
      "`autonome: false` dans `config/autonomie.yaml` (retour en simulation).")  # fmt: skip
    return "\n".join(L) + "\n"


def write_report(reports_dir: str | Path, d: CycleData, text: str) -> Path:
    """`cycle_<AAAA-MM-JJ>.md` (suffixe _2, _3... si plusieurs cycles le même jour) + copie
    `dernier_cycle.md`."""
    base = Path(reports_dir)
    base.mkdir(parents=True, exist_ok=True)
    stem = f"cycle_{d.date.strftime('%Y-%m-%d')}"
    path, k = base / f"{stem}.md", 2
    while path.exists():
        path, k = base / f"{stem}_{k}.md", k + 1
    path.write_text(text, encoding="utf-8")
    (base / "dernier_cycle.md").write_text(text, encoding="utf-8")
    return path
