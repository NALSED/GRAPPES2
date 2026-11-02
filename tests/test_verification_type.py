from grappes.decouverte.registres import Decouvert
from grappes.decouverte.verification_type import (
    COMPATIBLE,
    INCOMPATIBLE,
    INCONNU,
    demande_type,
    filtrer,
    verifier,
)

NAF = {"93.12": "x", "93.12Z": "Activités de clubs de sports", "84.11Z": "Administration publique générale", "90.01Z": "Arts du spectacle vivant"}


def d(nom, type_candidat="", nature=""):
    return Decouvert(nom, "x", nom, type_candidat=type_candidat, nature=nature)


def test_demande_type_premier_mot_plein_sans_complement():
    dem = demande_type("Les clubs de handball professionnels en France")
    assert dem.nature == "organisation"
    assert dem.vocabulaire == frozenset({"club"})  # « handball » est le domaine, pas le type


def test_type_declare_compatible_par_libelle_naf():
    dem = demande_type("Les clubs de handball professionnels en France")
    assert verifier(d("HAND 12", "93.12Z"), dem, NAF)[0] == COMPATIBLE


def test_type_declare_contradictoire_ecarte_meme_si_le_nom_porte_le_domaine():
    dem = demande_type("Les clubs de handball professionnels en France")
    v, motif = verifier(d("COMMUNE DE X HANDBALL", "84.11Z - Administration publique générale"), dem, NAF)
    assert v == INCOMPATIBLE and "Administration" in motif


def test_le_nom_n_est_pas_une_preuve_de_type():
    dem = demande_type("Les clubs de handball professionnels en France")
    assert verifier(d("SMILE COMEDY CLUB", "90.01Z"), dem, NAF)[0] == INCOMPATIBLE  # trouvé PAR le mot « club »


def test_personne_physique_ecartee():
    dem = demande_type("Troupe théâtrale en France")
    assert verifier(d("DUPONT", "90.01Z", "Personne physique autre que particulier employeur"), dem, NAF)[0] == INCOMPATIBLE


def test_sans_type_declare_inconnu_et_garde():
    dem = demande_type("Chorale en France")
    assert verifier(d("Chorale du lac"), dem, NAF)[0] == INCONNU
    gardes, ecartes = filtrer([d("A"), d("B", "84.11Z")], dem, NAF)
    assert [x.nom for x in gardes] == ["A"] and [x.nom for x, _ in ecartes] == ["B"]


def test_type_fixe_du_jeu_de_donnees():
    assert verifier(d("Fest", "festival"), demande_type("Les festivals de musique électronique en France"), NAF)[0] == COMPATIBLE
    assert verifier(d("Fest", "festival"), demande_type("Les clubs de handball professionnels en France"), NAF)[0] == INCOMPATIBLE
