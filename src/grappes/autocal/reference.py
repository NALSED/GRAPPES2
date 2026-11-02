"""Référence « silver standard » d'un sujet, par supervision distante sur Wikidata (Mintz et al. 2009).

Ce n'est PAS une vérité absolue (DECISION_AUTOCAL.md, méthode 2) : Wikidata est incomplet (hypothèse de base complète
fausse). Une entité absente de la référence n'est donc pas un faux ; `juger.py` tient trois comptes : juste, faux,
non vérifiable. La référence s'écrit au format qrels (TREC) : `sujet 0 Q123 1`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from grappes.autocal.wikidata import Wikidata
from grappes.canon import canonical_domain

FR = "Q142"


@dataclass(slots=True)
class RefEntity:
    qid: str
    label: str
    aliases: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    siren: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Reference:
    sujet_id: str
    phrase: str
    where: str  # clause SPARQL sur ?item (traçabilité)
    entities: list[RefEntity] = field(default_factory=list)
    classes: list[str] = field(
        default_factory=list
    )  # classes Wikidata acceptées pour juger (P31 direct)

    @property
    def r(self) -> int:
        return len(self.entities)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | Path) -> Reference:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        d["entities"] = [RefEntity(**e) for e in d["entities"]]
        return cls(**d)

    def write_qrels(self, path: str | Path, extra: dict[str, int] | None = None) -> None:
        """Format qrels TREC : `sujet 0 docid pertinence`. Référence = 1 ; faux jugés = 0 (via `extra`)."""
        lines = [f"{self.sujet_id} 0 {e.qid} 1" for e in sorted(self.entities, key=lambda e: e.qid)]
        lines += [f"{self.sujet_id} 0 {d} {rel}" for d, rel in sorted((extra or {}).items())]
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _domain(url: str) -> str:
    host = urlsplit(url if "://" in url else "http://" + url).hostname or ""
    return canonical_domain(host)


def build(w: Wikidata, sujet_id: str, phrase: str, where: str, classes: list[str]) -> Reference:
    """Entités de `where` (clause sur ?item) avec libellé fr (sinon en), alias fr, sites officiels (P856), SIREN (P1616)."""
    q_main = f"""SELECT ?item ?label ?site ?siren WHERE {{
  {where}
  OPTIONAL {{ ?item rdfs:label ?lf FILTER(LANG(?lf)="fr") }}
  OPTIONAL {{ ?item rdfs:label ?le FILTER(LANG(?le)="en") }}
  BIND(COALESCE(?lf, ?le) AS ?label)
  OPTIONAL {{ ?item wdt:P856 ?site }}
  OPTIONAL {{ ?item wdt:P1616 ?siren }}
}}"""
    q_alias = f"""SELECT ?item ?alias WHERE {{
  {where}
  ?item skos:altLabel ?alias FILTER(LANG(?alias)="fr")
}}"""
    ents: dict[str, RefEntity] = {}
    for row in w.sparql(q_main):
        label = row.get("label", "")
        if not label or label == row["item"]:
            continue  # sans libellé lisible : inutilisable pour juger
        e = ents.setdefault(row["item"], RefEntity(row["item"], label))
        if row.get("site"):
            d = _domain(row["site"])
            if d and d not in e.domains:
                e.domains.append(d)
        if row.get("siren") and row["siren"] not in e.siren:
            e.siren.append(row["siren"])
    for row in w.sparql(q_alias):
        e = ents.get(row["item"])
        if e and row["alias"] not in e.aliases and row["alias"] != e.label:
            e.aliases.append(row["alias"])
    return Reference(sujet_id, phrase, where, sorted(ents.values(), key=lambda e: e.qid), classes)


def class_where(class_qid: str) -> str:
    """Clause standard : instances directes de la classe, en France."""
    return f"?item wdt:P31 wd:{class_qid} ; wdt:P17 wd:{FR} ."
