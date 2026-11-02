"""Découverte par registres (D63, chantier 1) : traduction de la phrase, fusion, doublons. Aucun réseau."""

from grappes.decouverte.registres import Decouvert, DecouverteConfig, _filtre_nom, sirene
from grappes.decouverte.traduction import ClasseWikidata, classe_wikidata, code_naf, noyau, racine


class FauxHttp:
    """Réponses fixes : aucun réseau."""

    def __init__(self, hits: dict[str, list[dict]], instances: dict[str, int]):
        self.hits, self.instances = hits, instances

    def wd_search(self, texte, limite=8, langue="fr"):
        return self.hits.get(texte, [])

    def sparql(self, q):
        for qid, n in self.instances.items():
            if f"wd:{qid} " in q:
                return [{"n": str(n)}]
        return [{"n": "0"}]


def test_noyau_retire_articles_et_lieu():
    assert noyau("Les clubs de handball professionnels en France") == ["clubs", "handball", "professionnels"]
    assert noyau("Brasserie en France") == ["brasserie"]
    assert noyau("Club de rugby à XV en France") == ["club", "rugby", "xv"]
    assert noyau("Les écoles de sorcellerie de Haute-Savoie") == ["ecoles", "sorcellerie"]


def test_racine_ignore_pluriel():
    assert racine("clubs") == racine("club")
    assert racine("la") == "la"


def test_classe_exige_libelle_identique_et_instances():
    http = FauxHttp({"club de handball": [{"id": "Q1", "label": "club de handball"}]}, {"Q1": 12})
    c = classe_wikidata("Les clubs de handball en France", http)
    assert isinstance(c, ClasseWikidata) and c.qid == "Q1" and c.instances == 12
    # classe sans assez d'instances, ou trop générale : rien (on n'invente pas)
    assert classe_wikidata("Les clubs de handball", FauxHttp(http.hits, {"Q1": 1})) is None
    assert classe_wikidata("Les clubs de handball", FauxHttp(http.hits, {"Q1": 5000})) is None
    # libellé différent : rejeté
    assert classe_wikidata("Les clubs de handball", FauxHttp({"club de handball": [{"id": "Q2", "label": "handball"}]}, {"Q2": 50})) is None


def test_mot_unique_dune_expression_refuse():
    http = FauxHttp({"groupes": [{"id": "Q9", "label": "groupe"}]}, {"Q9": 11})
    assert classe_wikidata("Les groupes de supporters", http) is None


def test_code_naf_recouvrement():
    naf = [("11.05Z", "Fabrication de bière"), ("93.12Z", "Activités de clubs de sports")]
    assert code_naf("Fabrication de bière en France", naf).code == "11.05Z"
    assert code_naf("Les clubs de handball professionnels", naf) is None  # recouvrement insuffisant


def test_filtre_nom():
    assert _filtre_nom("FENIX TOULOUSE HANDBALL", ["handball"])
    assert not _filtre_nom("BOULANGERIE DUPONT", ["handball", "club"])


def test_sirene_repli_nom_filtre(monkeypatch):
    pages = {1: {"results": [
        {"siren": "111111111", "nom_complet": "CLUB DE HANDBALL DE X", "siege": {"libelle_commune": "X"}},
        {"siren": "222222222", "nom_complet": "BOULANGERIE", "siege": {}},
    ], "total_pages": 1}}

    class H:
        def get_json(self, url):
            return pages[1]

    out = sirene("Clubs de handball", None, H(), DecouverteConfig(pages_sirene=2))
    assert [d.siren for d in out] == ["111111111"] and out[0].ville == "X"
    assert isinstance(out[0], Decouvert)


def test_domaine_type_plus_complement_et_mots_ignores():
    from grappes.decouverte.traduction import domaine_wikidata, pays_de_la_phrase, singulier

    assert singulier("groupes") == "groupe" and singulier("bus") == "bus" and singulier("pas") == "pas"
    assert pays_de_la_phrase("Les clubs de handball en France") == "Q142"
    assert pays_de_la_phrase("Les festivals de jazz") == ""

    class H:
        def wd_search(self, texte, limite=8, langue="fr"):
            base = {"handball": [{"id": "Q8418", "label": "handball"}], "club": [{"id": "Q988108", "label": "club"}]}
            return base.get(texte, [])

    d = domaine_wikidata("Les clubs de handball professionnels en France", H())
    assert d is not None and d.entite == "Q8418" and d.ignores == ["professionnels"] and d.pays == "Q142"
    assert d.type_qid == "Q988108"
    # complément introuvable (même après retrait de deux mots) : rien, on n'invente pas
    assert domaine_wikidata("Les éleveurs de dragons verts en Corrèze", H()) is None
    # un seul mot : pas de complément
    assert domaine_wikidata("Brasserie en France", H()) is None


def test_wikidata_garde_les_alias_dans_les_liens_et_la_limite_borne_les_elements():
    """Document 24, point 2 : les alias (skos:altLabel) multiplient les lignes ; la LIMIT doit rester sur les ÉLÉMENTS et les alias vont dans `liens`."""
    from grappes.decouverte.registres import wikidata

    vues: list[str] = []

    class Http:
        def sparql(self, q):
            vues.append(q)
            return [{"item": "Q868550", "itemLabel": "Commissariat à l'énergie atomique", "alias": "CEA"},
                    {"item": "Q868550", "itemLabel": "Commissariat à l'énergie atomique", "alias": "Commissariat à l'énergie atomique"},  # le nom lui-même : écarté
                    {"item": "Q868550", "itemLabel": "Commissariat à l'énergie atomique", "alias": "AFNI"}]

    [d] = wikidata(ClasseWikidata("Q31855", "institut", "", 10, True), Http(), DecouverteConfig(limite_wikidata=50))
    assert d.liens["alias"] == '["CEA", "AFNI"]'
    assert "skos:altLabel" in vues[0] and "{ SELECT ?item WHERE" in vues[0] and vues[0].count("LIMIT 50") == 1
