"""Boucle automatique `grappes cycle` (FINAL.md § 8) : aucune requête réelle."""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outils"))
import corpus_synthetique as cs

from grappes.config import load_config
from grappes.cycle import engine
from grappes.cycle.engine import CANCELLED, DRY, FAILED, OK, STOPPED, rollback_last, run_cycle
from grappes.cycle.state import CycleBusy, cycle_lock, read_log
from grappes.learned import load_learned_lexicon
from grappes.pipeline import ingest
from grappes.storage.db import Database

ECOS = ("x", "telegram", "instagram", "web")
A, B, C = cs.A, cs.B, cs.C
EXTRA_ALL = {w for words in cs.EXTRA.values() for w in words}
NORMALISE = {"bêcher": "becher", "solfège": "solfege"}


class Monde:
    def __init__(self, tmp_path, autonome=True, piege=False, **auto):
        self.root = tmp_path
        self.out_corpus = tmp_path / "corpus"
        cs.write(self.out_corpus)
        if piege:
            self.piege = cs.ajouter_cercle_piege(self.out_corpus)
        self.config = self.out_corpus / "config"
        self.data = tmp_path / "donnees"
        self.db = self.data / "grappes.db"
        self.out = tmp_path / "sorties"
        self.stop = tmp_path / "STOP"
        self.rapports = tmp_path / "rapports"
        settings = {
            "autonome": autonome,
            "stop_file": str(self.stop),
            "rapports_dir": str(self.rapports),
            "stabilite": auto.pop("stabilite", {}),
            "apprentissage": auto.pop("apprentissage", {}),
            "recalibrage": auto.pop("recalibrage", {}),
            **auto,
        }
        (self.config / "autonomie.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
        cfg = load_config(self.config)
        db = Database(self.db)
        for eco in ECOS:
            ingest(db, self.out_corpus / eco, eco, cfg)
        db.close()

    def cycle(self, **kw):
        return run_cycle(self.config, self.db, self.out, self.data, **kw)

    def sednal_hash(self):
        """Empreinte de tout ce que Sednal écrit : la boucle n'y touche jamais."""
        h = hashlib.sha256()
        for p in sorted(self.config.rglob("*.yaml")):
            if "apprentissage" in p.parts:
                continue
            h.update(p.name.encode() + p.read_bytes())
        return h.hexdigest()

    def runs(self):
        if not self.db.exists():
            return 0
        c = sqlite3.connect(self.db)
        try:
            return c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            c.close()

    def learned(self, gid):
        return load_learned_lexicon(self.config, gid)


def separation(m: Monde) -> float:
    """Moyenne, sur les entités « cachées » (seulement du vocabulaire du thème absent des lexiques),
    de S(grappe juste) − S(meilleure autre grappe), d'après le dernier run enregistré."""
    cfg = load_config(m.config)
    c = sqlite3.connect(m.db)
    c.row_factory = sqlite3.Row
    try:
        run_id = c.execute(
            "SELECT run_id FROM runs ORDER BY ts DESC, rowid DESC LIMIT 1"
        ).fetchone()[0]
        total, n = 0.0, 0
        att = json.loads((m.out_corpus / "attendu.json").read_text("utf-8"))
        for gid, refs in att["cachees"].items():
            for ref in refs:
                rows = {
                    r["grappe_id"]: r["semantic"]
                    for r in c.execute(
                        "SELECT ms.grappe_id, ms.semantic FROM memberships ms JOIN entities e USING(entity_id)"
                        " WHERE e.primary_ref=? AND ms.run_id=?", (ref, run_id),
                    )
                }  # fmt: skip
                best_other = max((v for g, v in rows.items() if g != gid), default=0.0)
                total += rows.get(gid, 0.0) - best_other
                n += 1
        assert len(cfg.grappes) == 3
        return total / n
    finally:
        c.close()


# --- simulation, arrêts, verrou ---------------------------------------------------------------------


def test_simulation_tant_que_autonome_est_faux_rien_n_est_ecrit(tmp_path):
    m = Monde(tmp_path, autonome=False)
    avant = (m.sednal_hash(), m.runs(), sorted(p.name for p in m.root.rglob("*") if p.is_file()))
    out = m.cycle()
    assert out.status == DRY and out.code == 0 and "Simulation" in out.report
    assert "`autonome` est faux" in out.report
    apres = (m.sednal_hash(), m.runs(), sorted(p.name for p in m.root.rglob("*") if p.is_file()))
    assert (
        apres == avant
    )  # ni base, ni journal, ni verrou, ni rapport, ni instantané, ni mot appris
    assert not m.rapports.exists() and not (m.data / "cycles.jsonl").exists() and not m.out.exists()


def test_dry_run_explicite_meme_si_autonome(tmp_path):
    m = Monde(tmp_path, autonome=True)
    out = m.cycle(dry_run=True)
    assert out.status == DRY and m.runs() == 0 and not (m.data / "instantanes").exists()


def test_fichier_stop_refuse_le_cycle(tmp_path):
    m = Monde(tmp_path)
    m.stop.write_text("")
    out = m.cycle()
    assert (
        out.status == STOPPED
        and out.code == 4
        and m.runs() == 0
        and not (m.data / "cycles.jsonl").exists()
    )


def test_verrou_anti_concurrence(tmp_path):
    m = Monde(tmp_path)
    with cycle_lock(m.data / "cycle.lock"):
        out = m.cycle()
        assert out.status == "occupe" and out.code == 3 and m.runs() == 0
        with pytest.raises(CycleBusy), cycle_lock(m.data / "cycle.lock"):
            pass
    assert m.cycle().status == OK  # le verrou est libéré


# --- un cycle normal ---------------------------------------------------------------------------------


def test_cycle_complet_export_rapport_journal_instantane(tmp_path):
    m = Monde(tmp_path)
    sednal = m.sednal_hash()
    out = m.cycle()
    assert out.status == OK and out.code == 0 and out.n == 1
    assert (m.out / "resultat.csv").exists() and (m.out / "entites.csv").exists()
    assert (
        out.report_path.exists()
        and (m.rapports / "dernier_cycle.md").read_text("utf-8") == out.report
    )
    (rec,) = read_log(m.data / "cycles.jsonl")
    assert rec["status"] == OK and rec["run_id"] == out.run_id and rec["n"] == 1
    assert (m.data / "instantanes" / out.cycle_id / "meta.json").exists()
    assert m.sednal_hash() == sednal  # lexiques, ancres, configuration de Sednal : jamais modifiés


def test_apprentissage_uniquement_du_vocabulaire_des_confirmees(tmp_path):
    m = Monde(tmp_path)
    m.cycle()
    base = {A: set(cs.TERMS[A]), B: set(cs.TERMS[B]), C: set(cs.TERMS[C])}
    for gid, extra in ((A, cs.EXTRA[A]), (B, cs.EXTRA[B]), (C, cs.EXTRA[C])):
        learned = m.learned(gid)
        assert learned, gid
        assert set(learned) <= {NORMALISE.get(w, w) for w in extra}  # que du vocabulaire du thème
        assert not set(learned) & {"bonjour", "demain", "voiture", "lundi"}  # jamais du bruit
        assert not set(learned) & {t.lstrip("#") for t in base[gid]}  # jamais un terme de Sednal
        assert all(lt.weight == 0.3 for lt in learned.values())  # poids faible
        assert len(learned) <= 5  # plafond par cycle
    cfg = load_config(m.config)
    g = next(g for g in cfg.grappes if g.grappe_id == A)
    assert set(g.learned_terms) == set(m.learned(A))
    assert {t: w for t, w in g.lexicon.terms.items() if t in cs.TERMS[A]} == {
        t: 1.0 for t in cs.TERMS[A]
    }


def test_plafond_de_mots_par_cycle(tmp_path):
    m = Monde(tmp_path, apprentissage={"max_termes_par_cycle": 1})
    m.cycle()
    assert all(len(m.learned(g)) == 1 for g in (A, B, C))


def test_amelioration_de_la_separation_sur_trois_cycles(tmp_path):
    m = Monde(tmp_path, apprentissage={"max_termes_par_cycle": 1})
    out0 = m.cycle(dry_run=True)
    assert out0.status == DRY
    scores = []
    for _ in range(3):
        assert m.cycle().status == OK
        scores.append(separation(m))
    assert scores[0] > 0  # dès le premier cycle, un mot appris sépare déjà un peu
    assert scores == sorted(scores) and scores[-1] > scores[0]  # jamais de recul, progrès net
    assert all(len(m.learned(g)) == 3 for g in (A, B, C))  # un mot de plus par cycle


def test_idempotence_une_fois_stabilise(tmp_path):
    m = Monde(tmp_path)
    runs = [m.cycle() for _ in range(3)]
    assert all(r.status == OK for r in runs)
    csv1 = (m.out / "resultat.csv").read_bytes()
    entites1 = (m.out / "entites.csv").read_bytes()
    nb_mots = {g: set(m.learned(g)) for g in (A, B, C)}
    again = m.cycle()
    assert again.run_id == runs[-1].run_id  # mêmes données, mêmes mots : même run
    assert (m.out / "resultat.csv").read_bytes() == csv1 and (
        m.out / "entites.csv"
    ).read_bytes() == entites1
    assert {g: set(m.learned(g)) for g in (A, B, C)} == nb_mots


# --- corpus piégé ---------------------------------------------------------------------------------------


def test_cercle_de_fausses_sources_ne_contamine_pas_le_lexique(tmp_path):
    # support_min très bas : seul le garde-fou de la racine de confiance protège ici
    m = Monde(
        tmp_path,
        piege=True,
        apprentissage={"support_min": 0.1, "max_termes_par_cycle": 12},
        stabilite={
            "max_derive_lexique": 50.0
        },  # on veut observer l'apprentissage, pas la sécurité de dérive
    )
    cfg = load_config(m.config)
    db = Database(m.db)
    from grappes.pipeline import run

    _, res = run(db, cfg)  # le piège fonctionne : les fausses cibles sont bien « confirmées »
    ref_cibles = {f"web:{d}" for d in m.piege["cibles"]}
    assert all(any(r.cited == c and r.validated for r in res.validations) for c in ref_cibles)
    db.close()
    for _ in range(3):
        assert m.cycle().status == OK
    appris = {g: set(m.learned(g)) for g in (A, B, C)}
    assert not set().union(*appris.values()) & set(m.piege["spam"])  # aucun mot de spam appris
    assert set().union(*appris.values())  # mais les vrais mots, eux, sont appris
    assert "confirmée(s) fiable(s)" in (m.rapports / "dernier_cycle.md").read_text("utf-8")
    assert "écartée(s) faute de lien" in (m.rapports / "dernier_cycle.md").read_text("utf-8")


def test_sans_garde_fou_le_meme_piege_contaminerait(tmp_path):
    """Témoin : sans exigence de racine de confiance, le spam est appris. Le test précédent prouve donc
    quelque chose."""
    m = Monde(
        tmp_path,
        piege=True,
        apprentissage={
            "exiger_racine_de_confiance": False,
            "support_min": 0.1,
            "max_termes_par_cycle": 12,
        },
        stabilite={"max_derive_lexique": 50.0},
    )
    m.cycle()
    assert set(m.piege["spam"]) & set(m.learned(A))


# --- retour en arrière ----------------------------------------------------------------------------------


def _etat(m: Monde):
    h = hashlib.sha256()
    for base in (m.config / "apprentissage", m.out):
        for p in sorted(base.rglob("*")) if base.exists() else []:
            if p.is_file():
                h.update(p.name.encode() + p.read_bytes())
    return h.hexdigest(), m.runs()


def test_degradation_declenche_le_retour_en_arriere_automatique(tmp_path):
    m = Monde(tmp_path, stabilite={"max_taux_ambiguite": 0.0, "min_entites": 5})
    avant = _etat(m)
    out = m.cycle()
    assert out.status == CANCELLED and out.code == 5 and "taux_ambiguite" in out.indicators.exceeded
    assert _etat(m) == avant  # base, mots appris, seuils, sorties : tels qu'avant le cycle
    assert not (m.config / "apprentissage").exists() and m.runs() == 0
    texte = out.report
    assert (
        "CYCLE ANNULÉ" in texte
        and "Retour en arrière" in texte
        and "trop d'entités ambiguës" in texte
    )
    (rec,) = read_log(m.data / "cycles.jsonl")
    assert (
        rec["status"] == CANCELLED
        and rec["restaure"] == out.cycle_id
        and "taux_ambiguite" in rec["indicateurs"]["depasses"]
    )


def test_derive_du_lexique_declenche_le_retour_en_arriere(tmp_path):
    m = Monde(tmp_path, stabilite={"max_derive_lexique": 0.0, "min_entites": 5})
    out = m.cycle()
    assert out.status == CANCELLED and "derive_du_lexique" in out.indicators.exceeded
    assert not (m.config / "apprentissage").exists()


def test_indicateurs_de_stabilite_et_seuils():
    from grappes.autonomy import Autonomy
    from grappes.cycle.metrics import Metrics, compare

    auto = Autonomy(
        max_changement_grappe=0.25,
        max_taux_ambiguite=0.4,
        chute_max_confirmes=0.3,
        max_derive_lexique=1.0,
        min_entites=4,
    )
    avant = Metrics("R1", 10, 8, 1, 4, {f"web:s{i}": "A" for i in range(8)})
    stable = Metrics("R2", 10, 8, 1, 4, dict(avant.main))
    assert compare(avant, stable, auto, 2, 10).exceeded == []
    bouge = Metrics(
        "R2", 10, 8, 1, 4, {r: ("B" if i < 3 else "A") for i, r in enumerate(avant.main)}
    )
    assert compare(avant, bouge, auto, 0, 10).exceeded == ["changement_de_grappe"]  # 3/8 > 25 %
    flou = Metrics("R2", 10, 8, 5, 4, dict(avant.main))
    assert compare(avant, flou, auto, 0, 10).exceeded == ["taux_ambiguite"]  # 5/8 > 40 %
    perd = Metrics("R2", 10, 8, 1, 1, dict(avant.main))
    assert compare(avant, perd, auto, 0, 10).exceeded == ["chute_des_confirmes"]  # 50 % -> 12,5 %
    assert compare(avant, stable, auto, 11, 10).exceeded == [
        "derive_du_lexique"
    ]  # 110 % du lexique de base
    assert compare(Metrics(), stable, auto, 5, 0).exceeded == [
        "derive_du_lexique"
    ]  # appris sans lexique de base
    petit = Metrics(
        "R2", 3, 3, 3, 0, {"web:a": "A"}
    )  # trop peu d'entités pour juger : jamais d'alerte
    assert compare(avant, petit, auto, 99, 1).exceeded == []


def test_recalibrage_borne_ne_fait_que_relacher(tmp_path):
    from grappes.autonomy import Autonomy
    from grappes.clustering.assign import assign
    from grappes.cycle.learn import confirmed_by_grappe, recalibrate

    m = Monde(tmp_path)
    cfg = load_config(m.config)
    db = Database(m.db)
    res = assign(db.load_observations(), cfg)
    db.close()
    auto = Autonomy()
    conf = confirmed_by_grappe(res, auto)
    rc = recalibrate(res, cfg, auto, conf)
    assert (
        rc.after["seuil_min"] <= rc.before["seuil_min"]
        and rc.after["marge_ambiguite"] <= rc.before["marge_ambiguite"]
    )
    # seuil artificiellement sévère : relâché, d'au plus variation_max, jamais sous la borne basse
    sev = Autonomy(variation_max=2.0, bornes_seuil_min=(30.0, 60.0))
    import dataclasses

    cfg2 = dataclasses.replace(cfg, scoring=dataclasses.replace(cfg.scoring, seuil_min=60.0))
    rc2 = recalibrate(res, cfg2, sev, conf)
    assert 58.0 <= rc2.after["seuil_min"] <= 60.0
    assert not recalibrate(res, cfg, Autonomy(recalibrage_actif=False), conf).changed


def test_erreur_en_cours_de_cycle_restaure(tmp_path, monkeypatch):
    m = Monde(tmp_path)
    avant = _etat(m)

    def boom(*a, **k):
        raise RuntimeError("panne simulée")

    monkeypatch.setattr(engine, "learn_grappe", boom)
    out = m.cycle()
    assert (
        out.status == FAILED
        and out.code == 1
        and "RuntimeError" in out.report
        and _etat(m) == avant
    )


def test_arret_demande_pendant_le_cycle_restaure(tmp_path, monkeypatch):
    m = Monde(tmp_path)
    avant = _etat(m)
    vrai_run = engine.run

    def run_puis_stop(*a, **k):
        m.stop.write_text("")  # Sednal pose le fichier STOP pendant le cycle
        return vrai_run(*a, **k)

    monkeypatch.setattr(engine, "run", run_puis_stop)
    out = m.cycle()
    assert out.status == STOPPED and "Arrêt demandé" in out.report and _etat(m) == avant


def test_commande_rollback_annule_le_dernier_cycle(tmp_path):
    m = Monde(tmp_path)
    avant = _etat(m)
    ok = m.cycle()
    assert ok.status == OK and _etat(m) != avant
    r = rollback_last(m.config, m.db, m.out, m.data)
    assert r.status == OK and r.extra["annule"] == 1
    assert _etat(m) == avant and not (m.config / "apprentissage").exists()
    log = read_log(m.data / "cycles.jsonl")
    assert [x["status"] for x in log] == [OK, "rollback_manuel"]
    assert rollback_last(m.config, m.db, m.out, m.data).status == FAILED  # rien d'autre à annuler


def test_instantanes_conserves_en_nombre_borne(tmp_path):
    m = Monde(tmp_path, instantanes_conserves=2)
    for _ in range(4):
        assert m.cycle().status == OK
    assert len([p for p in (m.data / "instantanes").iterdir() if p.is_dir()]) == 2


# --- rapport lisible --------------------------------------------------------------------------------------


def test_rapport_lisible_sans_donnee_personnelle(tmp_path):
    m = Monde(tmp_path)
    m.cycle()
    t = m.cycle().report
    for titre in (
        "## Alertes",
        "## Sujets suivis",
        "## Ce qui est entré",
        "## Où en sont les entités",
        "## Ce qui a changé de grappe",
        "## Mots appris, seuils",
        "## Stabilité",
    ):
        assert titre in t, titre
    assert (
        "Alice Démo" not in t and "alice_cuisine" not in t and "Bob Démo" not in t
    )  # personnes masquées
    assert "Identités déduites d'un nom" in t  # canaux Telegram dont l'identité est une hypothèse
    assert "Une source qui n'est pas recoupée reste « probable » au mieux." in t
    assert "grappes rollback" in t and "donnees/STOP" in t


def test_avertissement_si_les_personnes_sont_incluses(tmp_path):
    m = Monde(tmp_path)
    p = m.config / "scoring.yaml"
    p.write_text(
        p.read_text("utf-8").replace("inclure_personnes: false", "inclure_personnes: true"), "utf-8"
    )
    t = m.cycle().report
    assert "ATTENTION" in t and "EN CLAIR" in t


def test_retention_appliquee_et_comptee(tmp_path):
    m = Monde(tmp_path)
    p = m.config / "scoring.yaml"
    p.write_text(p.read_text("utf-8").replace("retention_days: 90", "retention_days: 1"), "utf-8")
    out = m.cycle()  # les données du corpus datent de plus d'un jour : tout est purgé
    assert (
        out.status == OK
        and out.data.purged > 0
        and "supprimées par la durée de conservation" in out.report
    )


def test_aucune_requete_reelle_sans_fournisseur(tmp_path):
    m = Monde(tmp_path)
    out = m.cycle()
    assert out.data.collect.requests == 0 and out.data.collect.candidates == 0


# --- de bout en bout : sujet en une phrase -> amorçage -> recherche -> pages -> entités -> rapport ----


def test_cycle_complet_depuis_une_phrase(tmp_path):
    from fakes_pages import PagesServer

    class Horloge:
        t = 0.0

        def monotonic(self):
            return self.t

        def sleep(self, s):
            self.t += s

    h = Horloge()
    with PagesServer(robots=(200, "User-agent: *\nDisallow: /interdit/\n")) as srv:
        cfg = tmp_path / "config"
        (cfg / "recherche").mkdir(parents=True)
        (cfg / "recherche" / "api.yaml").write_text(
            f"id: api\ntype: http\nurl: {srv.base}/api\nparams: {{q: '{{sujet}}'}}\nresponse: {{items: results}}\n"
            "fields: {title: title, url: link, description: snippet}\n"
        )
        (cfg / "recherche.yaml").write_text("actifs: [api]\ndelai_s: 0\n")
        (cfg / "sujets.yaml").write_text(
            yaml.safe_dump(
                {"sujets": ["jardins partagés et potagers collectifs en ville"]}, allow_unicode=True
            )
        )
        (cfg / "autonomie.yaml").write_text(
            yaml.safe_dump(
                {
                    "autonome": True,
                    "stop_file": str(tmp_path / "STOP"),
                    "rapports_dir": str(tmp_path / "rapports"),
                    "profondeur": 2,
                    "interdits": {"motifs": ["motifinterdit"]},
                }
            )
        )
        kw = {"allow_private": True, "sleep": h.sleep, "monotonic": h.monotonic}

        # simulation d'abord : aucun appel, rien d'écrit
        sim = run_cycle(
            cfg, tmp_path / "d" / "g.db", tmp_path / "s", tmp_path / "d", dry_run=True, **kw
        )
        assert sim.status == DRY and srv.log == [] and "à amorcer d'abord" in sim.report

        out = run_cycle(cfg, tmp_path / "d" / "g.db", tmp_path / "s", tmp_path / "d", **kw)
        assert out.status == OK, out.report
        t = out.report
        assert "(amorcé pendant ce cycle)" in t and "Nouvelles sources" in t
        assert srv.hits("/hors-sujet") == 0 and srv.hits("/interdit/secret") == 0  # jamais visités
        assert any("x.com" not in r["path"] for r in srv.log)
        assert (
            "réseau social" in t and "motif interdit" in t and "robots.txt" in t
        )  # refus expliqués en clair
        assert "Sites qui ont refusé les visites" in t  # le 403 est signalé comme alerte
        c = out.data.collect
        assert c.observations_new >= 3 and c.requests >= 2
        # les sources viennent de la recherche (pas d'un fichier) : provenance « recherche » ou « exploration »
        db = Database(tmp_path / "d" / "g.db")
        origines = set(db.origins().values())
        db.close()
        assert origines <= {"recherche", "exploration"} and "recherche" in origines
        # la phrase seule a produit le lexique ; rien n'a été écrit dans les fichiers de Sednal
        cfg_final = load_config(cfg)
        (g,) = cfg_final.grappes
        assert g.subject and {"jardin", "potager", "collectif"} <= set(g.lexicon.terms)
        assert not (cfg / "grappes").exists()
        # sortie finale : noms lisibles, une colonne
        lignes = (tmp_path / "s" / "resultat.csv").read_text("utf-8").splitlines()
        assert lignes[0] == "NOM" and all("://" not in x for x in lignes)
        # second cycle : rien de nouveau à ingérer, mêmes résultats (idempotent)
        n_obs = len(Database(tmp_path / "d" / "g.db").load_observations())
        out2 = run_cycle(cfg, tmp_path / "d" / "g.db", tmp_path / "s", tmp_path / "d", **kw)
        assert out2.status == OK and out2.data.collect.observations_new == 0
        assert len(Database(tmp_path / "d" / "g.db").load_observations()) == n_obs
        assert "page déjà ingérée" in out2.report


# --- ligne de commande ----------------------------------------------------------------------------------


def _cli(m, *args):
    from grappes.cli import main

    return main(
        [
            "--db",
            str(m.db),
            "--config",
            str(m.config),
            *args,
            "--out",
            str(m.out),
            "--data",
            str(m.data),
        ]
    )


def test_cli_cycle_simulation_par_defaut(tmp_path, capsys):
    m = Monde(tmp_path, autonome=False)
    assert _cli(m, "cycle") == 0
    sortie = capsys.readouterr().out
    assert "Simulation" in sortie and "simulation : rien n'a été appelé ni écrit." in sortie
    assert m.runs() == 0 and not m.rapports.exists()


def test_cli_cycle_reel_puis_rollback(tmp_path, capsys):
    m = Monde(tmp_path)
    assert _cli(m, "cycle") == 0
    sortie = capsys.readouterr().out
    assert "Cycle terminé normalement" in sortie and "rapport :" in sortie
    assert (m.out / "resultat.csv").exists()
    assert _cli(m, "rollback") == 0 and "annulé" in capsys.readouterr().out
    assert not (m.out).exists() or not (m.out / "resultat.csv").exists()
    assert _cli(m, "rollback") == 1 and "aucun cycle à annuler" in capsys.readouterr().err


def test_cli_cycle_stop_et_occupe(tmp_path, capsys):
    m = Monde(tmp_path)
    m.stop.write_text("")
    assert _cli(m, "cycle") == 4 and "arrêt demandé" in capsys.readouterr().err
    m.stop.unlink()
    with cycle_lock(m.data / "cycle.lock"):
        assert _cli(m, "cycle") == 3 and "déjà en cours" in capsys.readouterr().err


def test_cli_cycle_annule_code_5(tmp_path, capsys):
    m = Monde(tmp_path, stabilite={"max_taux_ambiguite": 0.0, "min_entites": 5})
    assert _cli(m, "cycle") == 5 and "CYCLE ANNULÉ" in capsys.readouterr().out


# --- seuil d'ingestion auto-ajustable : tracé dans le rapport, restauré par le retour en arrière -------------


def test_seuil_d_ingestion_trace_dans_le_rapport_et_restaure_au_retour(tmp_path, monkeypatch):
    from grappes.autonomy import load_autonomy
    from grappes.cycle.learn import IngestionAdjust

    m = Monde(tmp_path, seuil_ingestion={"valeur": 20, "min": 5, "max": 40, "auto_ajustable": True})
    out = m.cycle()
    assert (
        out.status == OK
        and "Seuil d'ingestion inchangé (20)" in out.report
        and "trop peu" in out.report
    )

    monkeypatch.setattr(
        engine,
        "adjust_ingestion",
        lambda *a, **k: IngestionAdjust(
            20.0, 25.0, "seulement 1 source sur 6 rangée : trop de bruit"
        ),
    )
    out2 = m.cycle()
    assert out2.status == OK
    assert "Seuil d'ingestion modifié** : 20 → 25. Raison : seulement 1 source sur 6" in out2.report
    assert load_autonomy(m.config).seuil_ingestion == 25  # utilisé au cycle suivant
    rec = read_log(m.data / "cycles.jsonl")[-1]
    assert rec["seuil_ingestion"] == 25.0
    assert rollback_last(m.config, m.db, m.out, m.data).status == OK
    assert (
        load_autonomy(m.config).seuil_ingestion == 20
    )  # le retour en arrière restaure aussi le seuil
