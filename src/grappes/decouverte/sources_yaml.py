"""Sources de découverte décrites en YAML (D68, chantier A) : une prise = un fichier `config/recherche/<id>.yaml`
de `type: registre`. Aucun code propre à un sujet ni à une source : ce module lit la description (adresse, paramètres,
chemins des champs) et interroge.

Les prises viennent du catalogue des APIs et jeux de données de data.gouv.fr (serveur MCP officiel, outils
`search_dataservices` et `get_dataservice_openapi_spec`) ; l'agent lit la spécification puis écrit le YAML.

Formats : `json` (API JSON, dont SPARQL), `csv` (fichier téléchargé une fois, filtré en local).

Variables de requête, calculées de la seule phrase du sujet (jamais de la référence) :
* `{requete}` : le complément du type (« clubs de HANDBALL professionnels » -> « handball professionnels »), ou le type
  seul si la phrase n'a qu'un mot plein ; les mots sont ramenés au singulier ;
* `{requete_et}` : les mêmes mots entre guillemets reliés par AND (plein texte Virtuoso/Lucene) ;
* `{type}` : le premier mot plein.
Type (D69) : `type_fixe` (ce que le jeu recense) ou `fields.type` (activité de la ligne), `fields.nature` (nature
juridique) alimentent le vérificateur de type (`verification_type.py`).
Relaxation : si la requête ne rend rien, les derniers mots sont retirés un à un (au plus `relaxation`), puis le type
seul ; la requête utilisée est tracée dans `Decouvert.requete`.
"""

from __future__ import annotations

import csv
import io
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from grappes.decouverte.client import DecouverteError, Http
from grappes.decouverte.registres import Decouvert
from grappes.decouverte.traduction import noyau, racine, singulier
from grappes.search.paths import resolve_field, select
from grappes.semantic.scoring import normalize

TYPE = "registre"
_MOT = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(slots=True)
class Source:
    id: str
    url: str
    format: str = "json"
    params: dict[str, str] = field(default_factory=dict)
    items: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    pagination: dict[str, Any] = field(default_factory=dict)  # {param, taille, max} : décalage numérique
    relaxation: int = 2
    actif: bool = True
    delimiter: str = ";"
    encoding: str = "utf-8-sig"
    colonnes_recherche: list[str] = field(default_factory=list)  # csv : colonnes où chercher (vide = toutes)
    description: str = ""
    type_fixe: str = ""  # type de ce que la source recense, tel que son jeu de données le définit (« festival »)


def charger_sources(dossier: str | Path = "config/recherche") -> list[Source]:
    out = []
    for p in sorted(Path(dossier).glob("*.yaml")):
        y = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if y.get("type") != TYPE:
            continue
        out.append(Source(
            y["id"], y["url"], y.get("format", "json"), {k: str(v) for k, v in (y.get("params") or {}).items()},
            (y.get("response") or {}).get("items", ""), y.get("fields") or {}, y.get("pagination") or {},
            int(y.get("relaxation", 2)), bool(y.get("actif", True)),
            str(y.get("delimiter", ";")), str(y.get("encoding", "utf-8-sig")), list(y.get("colonnes_recherche") or []), str(y.get("description", "")),
            str(y.get("type_fixe", "")),
        ))  # fmt: skip
    return out


def mots_requete(phrase: str) -> tuple[list[str], str]:
    """(mots du complément au singulier, type) tirés de la seule phrase."""
    n = [singulier(m) for m in noyau(phrase)]
    if not n:
        return [], ""
    return (n[1:] or n), n[0]


def variantes_requete(phrase: str, relaxation: int) -> list[list[str]]:
    mots, typ = mots_requete(phrase)
    out: list[list[str]] = []
    cur = list(mots)
    for _ in range(relaxation + 1):
        if cur and cur not in out:
            out.append(list(cur))
        cur = cur[:-1]
    if typ and [typ] not in out:
        out.append([typ])
    return out


def _sans_guillemet(mot: str) -> str:
    return "".join(_MOT.findall(mot))


def _gabarit(texte: str, mots: list[str], typ: str) -> str:
    safe = [m for m in (_sans_guillemet(x) for x in mots) if m]
    et = " AND ".join('\\"%s\\"' % m for m in safe)
    return texte.replace("{requete_et}", et).replace("{requete}", " ".join(safe)).replace("{type}", _sans_guillemet(typ))


def _lignes_csv(src: Source, http: Http) -> list[dict[str, str]]:
    texte = http.get_text(src.url, src.encoding)
    return list(csv.DictReader(io.StringIO(texte), delimiter=src.delimiter))


def _filtre_csv(lignes: list[dict[str, str]], src: Source, mots: list[str]) -> list[dict[str, str]]:
    cibles = {racine(normalize(m)) for m in mots}
    out = []
    for r in lignes:
        valeurs = [r.get(c, "") for c in (src.colonnes_recherche or r.keys())]
        lm = {racine(m) for m in _MOT.findall(normalize(" ".join(v for v in valeurs if v)))}
        if cibles <= lm:
            out.append(r)
    return out


def _elements_json(src: Source, mots: list[str], typ: str, http: Http) -> list[Any]:
    items: list[Any] = []
    pas = src.pagination
    decalages = range(0, int(pas.get("max", 0)) + 1, int(pas["taille"])) if pas else [None]
    for d in decalages:
        params = {k: _gabarit(v, mots, typ) for k, v in src.params.items()}
        if d is not None:
            params[str(pas["param"])] = str(d)
        sep = "&" if "?" in src.url else "?"
        data = http.get_json(src.url + sep + urllib.parse.urlencode(params))
        page = select(data, src.items) if src.items else [data]
        items += page
        if not pas or len(page) < int(pas["taille"]):
            break
    return items


def interroger(src: Source, phrase: str, http: Http) -> list[Decouvert]:
    """Acteurs nommés que la source rend pour la phrase. Une erreur réseau rend une liste vide (source muette)."""
    lignes = _lignes_csv(src, http) if src.format == "csv" else None
    typ = mots_requete(phrase)[1]
    for mots in variantes_requete(phrase, src.relaxation):
        try:
            items = _filtre_csv(lignes, src, mots) if lignes is not None else _elements_json(src, mots, typ, http)
        except DecouverteError:
            return []
        if items:
            trace = f"{src.id} « {' '.join(mots)} »"
            out: dict[str, Decouvert] = {}
            for it in items:
                nom = resolve_field(it, src.fields.get("nom")).strip()
                ident = resolve_field(it, src.fields.get("id")) or nom
                if not nom:
                    continue
                out.setdefault(ident, Decouvert(
                    nom, src.id, ident, resolve_field(it, src.fields.get("ville")).strip(),
                    resolve_field(it, src.fields.get("site")).strip(),
                    re.sub(r"\D", "", resolve_field(it, src.fields.get("siren")))[:9], trace,
                    resolve_field(it, src.fields.get("type")).strip() or src.type_fixe,
                    resolve_field(it, src.fields.get("nature")).strip(),
                ))  # fmt: skip
            return sorted(out.values(), key=lambda d: d.identifiant)
    return []
