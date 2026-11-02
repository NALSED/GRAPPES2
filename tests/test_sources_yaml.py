"""Prises YAML de découverte (D68) : aucune règle propre à un sujet, relaxation, CSV local, lectures ambiguës."""

from grappes.decouverte.sources_yaml import Source, charger_sources, interroger, mots_requete, variantes_requete
from grappes.decouverte.traduction import lectures


class FauxHttp:
    def __init__(self, json_par_motif=None, texte=""):
        self.json_par_motif, self.texte, self.urls = json_par_motif or {}, texte, []

    def get_json(self, url):
        self.urls.append(url)
        for motif, rep in self.json_par_motif.items():
            if motif in url:
                return rep
        return {"results": []}

    def get_text(self, url, encoding="utf-8-sig"):
        return self.texte

    def wd_search(self, texte, limite=8, langue="fr"):
        return [
            {"id": "Q1", "label": "brasserie", "description": "fabrique de bière"},
            {"id": "Q2", "label": "Brasserie", "description": "type de restaurant"},
            {"id": "Q3", "label": "brasserie artisanale", "description": "autre"},
        ]

    def sparql(self, q):
        return [{"n": "5"}]


def test_mots_requete_depend_de_la_seule_phrase():
    assert mots_requete("Les clubs de handball professionnels en France") == (["handball", "professionnel"], "club")
    assert mots_requete("Brasserie en France") == (["brasserie"], "brasserie")


def test_relaxation_retire_les_derniers_mots_puis_le_type():
    v = variantes_requete("Les clubs de handball professionnels en France", 2)
    assert v == [["handball", "professionnel"], ["handball"], ["club"]]


def test_source_json_relaxee_et_tracee():
    src = Source("s", "https://x.test/api", params={"q": "{requete}"}, items="results", fields={"nom": "nom", "id": "id"})
    http = FauxHttp({"q=handball+professionnel": {"results": []}, "q=handball": {"results": [{"nom": "Club A", "id": "1"}]}})
    out = interroger(src, "Les clubs de handball professionnels en France", http)
    assert [d.nom for d in out] == ["Club A"] and out[0].registre == "s" and "handball" in out[0].requete


def test_source_csv_filtre_local():
    csv = "Nom;Ville\nFestival Electro Nord;Lille\nFestival Jazz;Nice\n"
    src = Source("c", "https://x.test/f.csv", format="csv", fields={"nom": "Nom", "ville": "Ville"})
    out = interroger(src, "Festivals electro", FauxHttp(texte=csv))
    assert [d.nom for d in out] == ["Festival Electro Nord"] and out[0].ville == "Lille"


def test_prises_du_depot_sont_des_yaml_valides():
    ids = {s.id for s in charger_sources("config/recherche")}
    assert {"bnf_organisations", "esr_etablissements", "culture_festivals", "culture_spectacles"} <= ids


def test_lectures_liste_sans_departager():
    lec = lectures("Brasserie en France", FauxHttp())
    assert [x.qid for x in lec] == ["Q1", "Q2"] and all(x.instances == 5 for x in lec)


def test_type_et_nature_declares_par_la_source_sont_recopies():
    csv_ = "nom;act;nat\nCLUB HAND;93.12Z;Personne morale\nCLUB DUPONT;90.01Z;Personne physique\n"
    src = Source("c", "http://x", "csv", fields={"nom": "nom", "id": "nom", "type": "act", "nature": "nat"}, relaxation=0)
    r = interroger(src, "Les clubs", FauxHttp(texte=csv_))
    assert {x.nom: (x.type_candidat, x.nature) for x in r} == {
        "CLUB HAND": ("93.12Z", "Personne morale"), "CLUB DUPONT": ("90.01Z", "Personne physique")}
    src2 = Source("c", "http://x", "csv", fields={"nom": "nom", "id": "nom"}, relaxation=0, type_fixe="festival")
    r2 = interroger(src2, "Festival", FauxHttp(texte="nom\nFestival du lac\n"))
    assert [x.type_candidat for x in r2] == ["festival"]
