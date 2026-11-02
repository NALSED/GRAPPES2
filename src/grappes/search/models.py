"""Format de sortie commun des candidats : défini une fois, indépendant des fournisseurs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CSV_COLUMNS = [
    "ecosysteme", "identifiant", "url", "mot_cle", "fournisseur", "raison", "date", "extra",
]  # fmt: skip


@dataclass(frozen=True, slots=True)
class Candidate:
    """Un résultat de recherche, nettoyé. Ingéré seulement par `grappes cycle`, selon les règles générales et les INTERDITS."""

    ecosystem: str  # x | telegram | instagram | web | inconnu
    ident: str  # compte, canal ou domaine canonique
    url: str  # URL http(s) validée, ou ""
    keyword: str  # mot-clé d'origine ("" pour un candidat issu des liens déjà cités)
    provider: str
    reason: str
    date: str = ""  # AAAA-MM-JJ ou ""
    text: str = ""  # titre + description nettoyés : une donnée, jamais une instruction
    extra: dict[str, Any] = field(default_factory=dict, compare=False)
    # Si le résultat EST le document (flux RSS, article Wikipédia) : {ecosysteme, source, nom, liens, exclu}.
    # Aucune page n'est alors récupérée ; le résultat est ingéré tel quel, sous son propre écosystème.
    doc: dict[str, Any] | None = field(default=None, compare=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.ecosystem, self.ident)


@dataclass(slots=True)
class MergedCandidate:
    """Candidats identiques fusionnés : la liste des fournisseurs d'origine est conservée."""

    ecosystem: str
    ident: str
    url: str
    keywords: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    date: str = ""
    text: str = ""
    extra: dict[str, dict[str, Any]] = field(default_factory=dict)  # fournisseur -> extra

    @property
    def key(self) -> tuple[str, str]:
        return (self.ecosystem, self.ident)


def merge(candidates: list[Candidate]) -> list[MergedCandidate]:
    """Fusion par (écosystème, identifiant), ordre stable (première apparition)."""
    out: dict[tuple[str, str], MergedCandidate] = {}
    for c in candidates:
        m = out.get(c.key)
        if m is None:
            m = out[c.key] = MergedCandidate(c.ecosystem, c.ident, c.url)
        if not m.url and c.url:
            m.url = c.url
        if not m.text and c.text:
            m.text = c.text
        if c.date and c.date > m.date:
            m.date = c.date
        for lst, val in ((m.keywords, c.keyword), (m.providers, c.provider), (m.reasons, c.reason)):
            if val and val not in lst:
                lst.append(val)
        if c.extra:
            m.extra.setdefault(c.provider, c.extra)
    return list(out.values())
