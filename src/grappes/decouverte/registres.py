"""Découvrir des acteurs nommés dans les registres et bases structurées (chantier 1, D63).

Sources (activables une à une dans `config/decouverte.yaml`) :
* `wikidata` : éléments de la classe retenue dans le pays (P31, option sous-classes P279*), avec site officiel (P856),
  SIREN (P1616) et commune (P159 siège, sinon P131) ;
* `sirene` : annuaire des entreprises (SIRENE et RNA, `recherche-entreprises.api.gouv.fr`), établissements ACTIFS,
  par code NAF recouvrant la phrase, sinon par nom ; commune du siège donnée par le registre.

Jamais de déduction : une commune absente du registre reste vide. Un nom est celui du registre, tel quel.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from grappes.decouverte.client import DecouverteError, Http
from grappes.decouverte.traduction import (
    LIENS_DOMAINE,
    ClasseWikidata,
    CodeNaf,
    DomaineWikidata,
    charger_naf,
    classe_wikidata,
    code_naf,
    domaine_wikidata,
    mots,
    noyau,
    racine,
)
from grappes.semantic.scoring import normalize

API_ENTREPRISES = "https://recherche-entreprises.api.gouv.fr/search"


@dataclass(slots=True)
class Decouvert:
    nom: str
    registre: str  # "wikidata" | "sirene"
    identifiant: str  # QID ou SIREN
    ville: str = ""
    domaine: str = ""  # site officiel quand le registre le donne
    siren: str = ""
    requete: str = ""  # trace de la traduction (classe retenue, code NAF...)
    type_candidat: str = ""  # type ou activité déclaré par la source elle-même (code NAF, discipline, type de jeu)
    nature: str = ""  # nature juridique déclarée (« personne physique »...), vide si la source n'en dit rien
    liens: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DecouverteConfig:
    sources: tuple[str, ...] = ("wikidata", "sirene")
    pays_wikidata: str = "Q142"
    min_instances: int = 3
    sous_classes: bool = False
    limite_wikidata: int = 3000
    seuil_naf: float = 0.6
    pages_sirene: int = 8
    par_page: int = 25
    domaine: bool = True  # gabarit « type + complément » (clubs de HANDBALL) en plus de la classe
    repli_nom: bool = True  # sans code NAF fiable : recherche par nom, filtrée sur les mots de la phrase
    naf: str = "config/naf_rev2.csv"
    dossier_sources: str = "config/recherche"  # prises YAML (`type: registre`), activées par « yaml » dans `sources`
    ids_yaml: tuple[str, ...] = ()  # vide = toutes les prises actives du dossier
    verifier_type: bool = True  # D69 : écarter en amont les candidats des prises dont le type déclaré contredit le sujet


# Les autres noms que Wikidata déclare pour l'élément (« CEA » est un alias du Commissariat) : une PREUVE de nom, jamais une identité (document 24, point 2).
ALIAS_WIKIDATA = 'OPTIONAL { ?item skos:altLabel ?alias . FILTER(LANG(?alias) IN ("fr", "en")) } '


def _alias(d: Decouvert, r: dict[str, str]) -> None:
    """Range l'alias d'une ligne de réponse dans `liens["alias"]` (liste JSON), sans doublon ni le nom lui-même."""
    a = r.get("alias", "").strip()
    if not a or normalize(a) == normalize(d.nom):
        return
    vus = json.loads(d.liens.get("alias", "[]"))
    if a not in vus and len(vus) < 12:
        d.liens["alias"] = json.dumps([*vus, a], ensure_ascii=False)


def wikidata(cls: ClasseWikidata, http: Http, cfg: DecouverteConfig) -> list[Decouvert]:
    lien = "wdt:P31/wdt:P279*" if cfg.sous_classes else "wdt:P31"
    q = (  # sous-requête : la LIMIT borne les ÉLÉMENTS, pas les lignes que les alias multiplient ensuite
        (
            "SELECT ?item ?itemLabel ?site ?siren ?villeLabel ?alias WHERE { { SELECT ?item WHERE { ?item %s wd:%s ; wdt:P17 wd:%s } LIMIT %d } "
            "OPTIONAL { ?item wdt:P856 ?site } OPTIONAL { ?item wdt:P1616 ?siren } "
            "OPTIONAL { ?item wdt:P159|wdt:P131 ?ville } " + ALIAS_WIKIDATA + 'SERVICE wikibase:label { bd:serviceParam wikibase:language "fr,en" } }'
        )
        % (lien, cls.qid, cfg.pays_wikidata, cfg.limite_wikidata)
    )
    par: dict[str, Decouvert] = {}
    for r in http.sparql(q):
        qid = r["item"]
        nom = r.get("itemLabel", "")
        if not nom or nom == qid:  # pas de libellé lisible : rien à rendre
            continue
        d = par.setdefault(qid, Decouvert(nom, "wikidata", qid, requete=f"{cls.qid} « {cls.libelle} »"))
        if r.get("site") and not d.domaine:
            d.domaine = r["site"]
        if r.get("siren") and not d.siren:
            d.siren = r["siren"].replace(" ", "")
        v = r.get("villeLabel", "")
        if v and not v.startswith("Q") and not d.ville:
            d.ville = v
        _alias(d, r)
    return sorted(par.values(), key=lambda d: d.identifiant)


ORGANISATION = "Q43229"


def _details(items: list[str], http: Http, requete: str) -> list[Decouvert]:
    out: list[Decouvert] = []
    for i in range(0, len(items), 150):
        lot = items[i : i + 150]
        q = (
            "SELECT ?item ?itemLabel ?site ?siren ?villeLabel ?alias WHERE { VALUES ?item { %s } "
            "OPTIONAL { ?item wdt:P856 ?site } OPTIONAL { ?item wdt:P1616 ?siren } "
            "OPTIONAL { ?item wdt:P159|wdt:P131 ?ville } " + ALIAS_WIKIDATA + 'SERVICE wikibase:label { bd:serviceParam wikibase:language "fr,en" } }'
        ) % " ".join("wd:" + x for x in lot)
        par: dict[str, Decouvert] = {}
        for r in http.sparql(q):
            nom = r.get("itemLabel", "")
            if not nom or nom == r["item"]:
                continue
            d = par.setdefault(r["item"], Decouvert(nom, "wikidata", r["item"], requete=requete))
            d.domaine = d.domaine or r.get("site", "")
            d.siren = d.siren or r.get("siren", "").replace(" ", "")
            v = r.get("villeLabel", "")
            if v and not v.startswith("Q"):
                d.ville = d.ville or v
            _alias(d, r)
        out += list(par.values())
    return out


def wikidata_domaine(dom: DomaineWikidata, http: Http, cfg: DecouverteConfig) -> list[Decouvert]:
    """Organisations rattachées au domaine (sport, genre, secteur…), dans le pays de la phrase quand elle en donne un.

    Gabarit unique pour tous les sujets : propriété de rattachement (liste fixe) -> complément ou l'un de ses
    sous-concepts. Sont gardés les éléments dont une classe est une organisation ou une sous-classe du type du sujet
    (sinon : saisons, matchs, personnes…)."""
    pays = f"?item wdt:P17 wd:{dom.pays} ." if dom.pays else ""
    classes: dict[str, set[str]] = {}
    for p in LIENS_DOMAINE:
        for chemin in ("?g wdt:P279* wd:%s .", "FILTER(?g = wd:%s)"):
            q = "SELECT DISTINCT ?item ?c WHERE { ?item wdt:%s ?g . %s %s ?item wdt:P31 ?c } LIMIT %d" % (
                p, chemin % dom.entite, pays, cfg.limite_wikidata * 4,
            )  # fmt: skip
            try:
                rows = http.sparql(q)
            except DecouverteError:
                continue  # le chemin transitif peut dépasser le délai du service : repli sur le lien direct
            for r in rows:
                classes.setdefault(r["item"], set()).add(r["c"])
            break
    cibles = [ORGANISATION] + ([dom.type_qid] if dom.type_qid else [])
    ok: dict[str, bool] = {}
    for c in sorted({c for cs in classes.values() for c in cs}):
        q = "SELECT ?x WHERE { wd:%s wdt:P279* ?x . VALUES ?x { %s } } LIMIT 1" % (c, " ".join("wd:" + t for t in cibles))
        try:
            ok[c] = bool(http.sparql(q))
        except DecouverteError:
            ok[c] = False
    gardes = sorted(i for i, cs in classes.items() if any(ok.get(c) for c in cs))
    return _details(gardes, http, f"domaine {dom.entite} « {dom.libelle} »")


def _filtre_nom(nom_registre: str, requis: list[str]) -> bool:
    """Le nom contient au moins la moitié (arrondie en haut) des mots distinctifs demandés (racines)."""
    lm = {racine(m) for m in mots(nom_registre)}
    return sum(1 for m in requis if racine(m) in lm) >= max(1, -(-len(requis) // 2))


def sirene(phrase: str, naf: CodeNaf | None, http: Http, cfg: DecouverteConfig) -> list[Decouvert]:
    """Établissements actifs (sièges) : par code NAF si fiable, sinon par nom (repli)."""
    params: dict[str, str] = {"etat_administratif": "A", "per_page": str(cfg.par_page)}
    requis: list[str] = []
    if naf is not None:
        params["activite_principale"] = naf.code
        if naf.reste:
            params["q"] = " ".join(naf.reste)
            requis = naf.reste
        trace = f"NAF {naf.code} « {naf.libelle} »" + (f" + nom « {params['q']} »" if "q" in params else "")
    elif cfg.repli_nom:
        requis = noyau(phrase)
        if not requis:
            return []
        params["q"] = " ".join(requis)
        trace = f"nom « {params['q']} »"
    else:
        return []
    out: dict[str, Decouvert] = {}
    for page in range(1, cfg.pages_sirene + 1):
        url = API_ENTREPRISES + "?" + urllib.parse.urlencode({**params, "page": str(page)})
        try:
            d = http.get_json(url)
        except DecouverteError:
            break
        res = d.get("results", []) if isinstance(d, dict) else []
        for e in res:
            nom = e.get("nom_complet") or e.get("nom_raison_sociale") or ""
            siren = e.get("siren", "")
            if not nom or not siren or (requis and not _filtre_nom(nom, requis)):
                continue
            ident = (e.get("complements") or {}).get("identifiant_association") or siren
            out.setdefault(
                siren,
                Decouvert(
                    nom, "sirene", ident, (e.get("siege") or {}).get("libelle_commune") or "",
                    siren=siren, requete=trace,
                ),
            )  # fmt: skip
        if len(res) < cfg.par_page or page >= (d.get("total_pages") or page):
            break
    return sorted(out.values(), key=lambda x: x.siren)


def decouvrir(phrase: str, http: Http, cfg: DecouverteConfig | None = None) -> list[Decouvert]:
    cfg = cfg or DecouverteConfig()
    out: list[Decouvert] = []
    if "wikidata" in cfg.sources:
        # priorité : classe qui couvre TOUTE la phrase > gabarit « type + domaine » > classe partielle
        cls = classe_wikidata(phrase, http, cfg.pays_wikidata, cfg.min_instances)
        dom = domaine_wikidata(phrase, http) if cfg.domaine and not (cls and cls.complet) else None
        try:
            dec = wikidata_domaine(dom, http, cfg) if dom is not None else []
            if not dec and cls is not None:
                dec = wikidata(cls, http, cfg)
            out += dec
        except DecouverteError:
            pass
    if "sirene" in cfg.sources:
        naf = code_naf(phrase, charger_naf(Path(cfg.naf)), cfg.seuil_naf)
        out += sirene(phrase, naf, http, cfg)
    if "yaml" in cfg.sources:  # prises décrites en YAML (data.gouv.fr, D68) : aucune ne dérive de Wikidata
        from grappes.decouverte.sources_yaml import charger_sources, interroger

        from grappes.decouverte.verification_type import demande_type, filtrer

        yaml_out: list[Decouvert] = []
        for s in charger_sources(cfg.dossier_sources):
            if s.actif and (not cfg.ids_yaml or s.id in cfg.ids_yaml):
                yaml_out += interroger(s, phrase, http)
        if cfg.verifier_type:  # avant le classement, pas après coup : le type vérifié ici, jamais un filtre sur le score
            yaml_out, _ = filtrer(yaml_out, demande_type(phrase), charger_naf(Path(cfg.naf)))
        out += yaml_out
    return out
