"""Juge les résultats contre la référence silver standard : TROIS comptes, jamais deux (DECISION_AUTOCAL.md).

* juste : le résultat est une entité de la référence (domaine = site officiel P856, ou libellé/alias exact), ou un
  élément Wikidata de la classe attendue trouvé par libellé exact ;
* faux : le libellé exact du résultat désigne un ou plusieurs éléments Wikidata dont AUCUN n'est de la classe
  attendue (ni sous-classe) : le résultat est autre chose (un média, une personne...) ;
* non vérifiable : tout le reste. Wikidata est incomplet (hypothèse de base complète fausse) : une entité absente
  de la référence n'est pas un faux. Un non vérifiable ne compte JAMAIS comme juste.

Aucune correspondance approchée : libellés égaux après normalisation, ou domaines égaux. Les verdicts sont mis en
cache sur disque (un nom est jugé une seule fois, quel que soit le nombre d'essais d'optimisation).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from grappes.autocal.reference import Reference
from grappes.autocal.wikidata import Wikidata, WikidataError
from grappes.canon import canonical_domain, site_key
from grappes.semantic.scoring import normalize

JUSTE, FAUX, NON_VERIFIABLE = "juste", "faux", "non_verifiable"


def norm_name(name: str) -> str:
    """Casse, accents, ponctuation et espaces neutralisés (comparaison de libellés exacts)."""
    return re.sub(r"[^\w]+", " ", normalize(name)).strip()


@dataclass(slots=True)
class Verdict:
    verdict: str
    docid: str  # QID si résolu, sinon « x:<clé de source> »
    raison: str


class Juge:
    def __init__(self, ref: Reference, w: Wikidata, cache_path: str | Path):
        self.ref, self.w = ref, w
        self.cache_path = Path(cache_path)
        self._cache: dict[str, dict] = {}
        if self.cache_path.exists():
            self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.by_domain: dict[str, str] = {}
        self.by_label: dict[str, str] = {}
        self.by_siren: dict[str, str] = {}
        self.qids = {e.qid for e in ref.entities}
        for e in ref.entities:
            for d in e.domains:
                self.by_domain.setdefault(site_key(d), e.qid)
            for sn in e.siren:
                self.by_siren.setdefault(sn.replace(" ", "")[:9], e.qid)
            for lab in [e.label, *e.aliases]:
                self.by_label.setdefault(norm_name(lab), e.qid)
        self._compat: dict[str, bool] = {}

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._cache, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )

    # --- compatibilité de classe -------------------------------------------------------------

    def _class_ok(self, p31: list[str]) -> bool:
        """Une des classes `p31` est la classe attendue ou l'une de ses sous-classes (P279*)."""
        todo = [c for c in p31 if c not in self._compat]
        accepted = set(self.ref.classes)
        for c in todo:
            self._compat[c] = c in accepted
        todo = [c for c in todo if not self._compat[c]]
        if todo and accepted:
            q = (
                "SELECT DISTINCT ?c WHERE { VALUES ?c { %s } VALUES ?t { %s } ?c wdt:P279* ?t . }"
                % (
                    " ".join("wd:" + c for c in todo),
                    " ".join("wd:" + c for c in sorted(accepted)),
                )
            )
            ok = {r["c"] for r in self.w.sparql(q)}
            for c in todo:
                self._compat[c] = c in ok
        return any(self._compat.get(c, False) for c in p31)

    def _entities(self, qids: list[str]) -> dict[str, dict]:
        d = self.w.api(
            action="wbgetentities",
            ids="|".join(qids),
            props="claims|labels|aliases",
            languages="fr|en",
        )
        return d.get("entities", {})

    @staticmethod
    def _claim_ids(ent: dict, prop: str) -> list[str]:
        out = []
        for c in ent.get("claims", {}).get(prop, []):
            v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
            if isinstance(v, dict) and "id" in v:
                out.append(v["id"])
        return out

    # --- verdict -------------------------------------------------------------------------------

    def juger(self, name: str, source_key: str) -> Verdict:
        ck = f"{source_key}\x1f{name}"
        if ck in self._cache:
            return Verdict(**self._cache[ck])
        v = self._juger(name, source_key)
        self._cache[ck] = asdict(v)
        return v

    def _juger(self, name: str, source_key: str) -> Verdict:
        eco, _, ident = source_key.partition(":")
        if eco in ("web", "rss"):
            q = self.by_domain.get(site_key(canonical_domain(ident)))
            if q:
                return Verdict(JUSTE, q, "domaine = site officiel de la référence")
        if eco == "reg":  # entité d'un registre : « reg:<registre>:<identifiant>[|siren]»
            reg, _, rest = ident.partition(":")
            ident_reg, _, siren = rest.partition("|")
            if reg == "wikidata" and ident_reg in self.qids:
                return Verdict(JUSTE, ident_reg, "élément Wikidata de la référence (circulaire : découvert par Wikidata)")
            q = self.by_siren.get(siren or (ident_reg if reg == "sirene" else ""))
            if q:
                return Verdict(JUSTE, q, "SIREN de la référence")
        q = self.by_label.get(norm_name(name))
        if q:
            return Verdict(JUSTE, q, "libellé exact de la référence")
        try:
            hits = [
                h
                for h in self.w.search(name, 8)
                if norm_name(h.get("label", "")) == norm_name(name)
            ]
            if not hits:
                return Verdict(
                    NON_VERIFIABLE, "x:" + source_key, "aucun élément Wikidata de ce libellé"
                )
            ents = self._entities([h["id"] for h in hits[:5]])
        except WikidataError as exc:
            return Verdict(NON_VERIFIABLE, "x:" + source_key, f"Wikidata indisponible ({exc})")
        compatibles, others = [], []
        for qid, ent in ents.items():
            (compatibles if self._class_ok(self._claim_ids(ent, "P31")) else others).append(qid)
        if compatibles:
            return Verdict(
                NON_VERIFIABLE,
                "x:" + source_key,
                f"de la bonne classe mais hors référence ({compatibles[0]})",
            )
        return Verdict(FAUX, others[0], "libellé exact d'éléments d'une autre classe")
