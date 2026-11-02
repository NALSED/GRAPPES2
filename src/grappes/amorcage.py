"""Amorçage d'un sujet à partir d'une phrase (FINAL.md § 5).

La phrase est envoyée à la multiprise de recherche ; les titres et descriptions reçus (des DONNÉES non
fiables : on n'en lit que des mots) fournissent des termes discriminants, qui forment un lexique
initial marqué « amorçage » : poids faible et plafonné, nombre de termes plafonné. Aucun mot n'est
imposé par Sednal. La boucle `grappes cycle` l'affine ensuite.

Garde-fous : un terme doit apparaître dans au moins deux résultats distincts (un seul résultat piégé ne
suffit pas) ; mots vides exclus ; mots trop répandus exclus ; lettres seulement, 4 à 30 caractères.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from grappes.learned import LearnedTerm, save_seed
from grappes.semantic.scoring import normalize

MAX_WEIGHT = 0.5  # plafond absolu d'un poids d'amorçage
_WORD = re.compile(r"[^\W\d_]{4,30}(?:-[^\W\d_]{2,30})?", re.UNICODE)

# Mots vides courants (français, anglais) : sans fond de comparaison, on les écarte d'office.
STOPWORDS = frozenset(
    [
        "avec",
        "dans",
        "pour",
        "plus",
        "vous",
        "nous",
        "sont",
        "être",
        "deux",
        "trois",
        "quatre",
        "cinq",
        "plusieurs",
        "quelques",
        "certains",
        "certaines",
        "chaque",
        "moment",
        "moments",
        "lundi",
        "mardi",
        "mercredi",
        "jeudi",
        "vendredi",
        "samedi",
        "dimanche",
        "janvier",
        "février",
        "fevrier",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "aout",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
        "decembre",
        "hier",
        "demain",
        "aujourd",
        "semaine",
        "semaines",
        "année",
        "annee",
        "années",
        "annees",
        "jour",
        "jours",
        "d'abord",
        "toujours",
        "jamais",
        "parce",
        "puisque",
        "avait",
        "avaient",
        "était",
        "etait",
        "étaient",
        "etaient",
        "serait",
        "seraient",
        "sera",
        "seront",
        "aura",
        "auront",
        "avoir",
        "fait",
        "faits",
        "disait",
        "voici",
        "cela",
        "celles",
        "lequel",
        "laquelle",
        "lesquels",
        "cette",
        "ceux",
        "elle",
        "elles",
        "leur",
        "leurs",
        "tout",
        "tous",
        "toute",
        "toutes",
        "comme",
        "mais",
        "sans",
        "sous",
        "sur",
        "entre",
        "vers",
        "chez",
        "très",
        "aussi",
        "alors",
        "donc",
        "ainsi",
        "dont",
        "où",
        "cela",
        "celle",
        "celui",
        "ces",
        "ses",
        "son",
        "notre",
        "votre",
        "nos",
        "vos",
        "mes",
        "mon",
        "ton",
        "ton",
        "fait",
        "faire",
        "peut",
        "peuvent",
        "même",
        "autre",
        "autres",
        "encore",
        "déjà",
        "depuis",
        "après",
        "avant",
        "pendant",
        "selon",
        "contre",
        "parmi",
        "lors",
        "afin",
        "quand",
        "lorsque",
        "puis",
        "voir",
        "voici",
        "voila",
        "site",
        "page",
        "pages",
        "accueil",
        "article",
        "articles",
        "lire",
        "suite",
        "plus",
        "moins",
        "haut",
        "bas",
        "menu",
        "liens",
        "lien",
        "contact",
        "mentions",
        "légales",
        "that",
        "this",
        "with",
        "from",
        "have",
        "been",
        "were",
        "will",
        "would",
        "there",
        "their",
        "they",
        "them",
        "then",
        "than",
        "what",
        "when",
        "where",
        "which",
        "while",
        "about",
        "also",
        "into",
        "more",
        "most",
        "other",
        "over",
        "some",
        "such",
        "only",
        "very",
        "your",
        "yours",
        "ours",
        "here",
        "these",
        "those",
        "home",
        "page",
        "click",
        "read",
        "more",
        "menu",
        "links",
        "link",
        "contact",
        "terms",
        "privacy",
        "policy",
        "cookies",
    ]
)
_NORMALIZED_STOP = frozenset(normalize(w) for w in STOPWORDS)


def tokens(text: str) -> list[str]:
    """Mots candidats d'un texte : lettres seulement, minuscules, accents latins ôtés, sans mots vides."""
    return [w for w in _WORD.findall(normalize(text)) if w not in _NORMALIZED_STOP]


def singular(word: str) -> str:
    """Singulier approximatif (pluriels en -s ou -x d'un mot d'au moins 5 lettres), `""` sinon.
    Le scoring compare des mots entiers : sans cela « jardin » ne reconnaîtrait pas « jardins »."""
    if len(word) >= 5 and word.endswith(("s", "x")) and not word.endswith(("ss", "us")):
        return word[:-1]
    return ""


@dataclass(slots=True)
class Seed:
    terms: dict[str, float] = field(default_factory=dict)  # terme -> poids
    n_documents: int = 0
    reason: str = ""  # pourquoi vide, le cas échéant


def extract_terms(
    texts: list[str], phrase: str = "", max_terms: int = 20, weight: float = 0.4
) -> Seed:
    """Termes discriminants d'un ensemble de textes (titres + descriptions de résultats).

    * les mots de la phrase du sujet comptent toujours (s'ils ne sont pas des mots vides) ;
    * les autres doivent figurer dans au moins deux textes distincts et dans moins de 80 % d'entre eux
      (au-delà ils ne discriminent rien) ;
    * poids : au plus `weight` (et jamais plus de MAX_WEIGHT), plus faible pour les termes rares.
    """
    weight = min(weight, MAX_WEIGHT)
    docs = [set(tokens(t)) for t in texts if t and t.strip()]
    n = len(docs)
    seed = Seed(n_documents=n)
    phrase_words = list(dict.fromkeys(tokens(phrase)))
    df: Counter[str] = Counter(w for d in docs for w in d)
    tf: Counter[str] = Counter(w for t in texts if t for w in tokens(t))
    if n == 0 and not phrase_words:
        seed.reason = "aucun résultat exploitable et phrase sans mot significatif"
        return seed
    chosen: dict[str, float] = {}
    for w in phrase_words:  # chaque mot de la phrase, et son singulier (« jardins » -> « jardin »)
        for form in (w, singular(w)):
            if form and len(chosen) < max_terms:
                chosen.setdefault(form, weight)
    common_cut = (
        0.8 * n if n >= 5 else n + 1
    )  # trop répandu = ne discrimine pas (si assez de textes)
    ranked = sorted(
        (w for w, c in df.items() if c >= 2 and c <= common_cut and w not in chosen),
        key=lambda w: (-df[w], -tf[w], w),
    )
    for w in ranked:
        if len(chosen) >= max_terms:
            break
        chosen[w] = round(min(weight, weight * (0.5 + 0.5 * df[w] / n)), 4)
    seed.terms = chosen
    if not chosen:
        seed.reason = "aucun terme ne se répète dans au moins deux résultats"
    return seed


def amorcer(
    subject_id: str,
    phrase: str,
    config_dir: str | Path,
    texts: list[str],
    max_terms: int = 20,
    weight: float = 0.4,
    cycle: int = 0,
) -> Seed:
    """Extrait les termes de `texts`, les écrit dans `amorcage_<sujet>.yaml` (sans toucher aux
    fichiers de Sednal) et retourne l'amorçage. Rien n'est écrit si aucun terme n'est trouvé."""
    seed = extract_terms(texts, phrase, max_terms, weight)
    if seed.terms:
        save_seed(
            config_dir,
            subject_id,
            {t: LearnedTerm(w, cycle, cycle) for t, w in seed.terms.items()},
        )
    return seed
