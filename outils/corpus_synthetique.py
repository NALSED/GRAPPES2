"""Corpus synthétique reproductible : quelques centaines d'observations, 3 grappes, 4 écosystèmes.

Usage : .venv/bin/python outils/corpus_synthetique.py <dossier_sortie> [graine]

Écrit, au format des vrais exports (API X v2, export Telegram Desktop, API Instagram Graph, web) :
`x/`, `telegram/`, `instagram/`, `web/`, une configuration `config/` (3 grappes, table Telegram)
et `attendu.json` (ce que la chaîne complète doit produire). Aucune donnée réelle : thèmes et noms
neutres inventés. Mêmes fichiers à chaque génération (graine fixe).

Grappes : cuisine (A), jardinage (B, opposée à A), musique (C). Le terme « marché » est commun à A
et B (chevauchement). Cas couverts : recoupements validés, refusés (un seul écosystème, trop peu de
citants, citants non membres, citations hors fenêtre), ambiguïtés (opposition, marge), cercle de
sources neutres qui se citent, liens non identifiables (invitations, publications, raccourcis),
personnes et comptes institutionnels, canaux Telegram connus, inconnus, homonymes et accentués.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

A, B, C = "G-CUISINE", "G-JARDINAGE", "G-MUSIQUE"
TERMS = {
    A: ["soupe", "recette", "#cuisine", "café crème", "marché"],
    B: ["potager", "semis", "tomates", "#jardinage", "marché"],
    C: ["concert", "guitare", "#musique", "partition", "orchestre"],
}
# Vocabulaire du thème absent des lexiques : ce que la boucle d'apprentissage doit découvrir.
EXTRA = {
    A: ["mijoter", "bouillon", "casserole", "gratin"],
    B: ["bêcher", "arrosoir", "paillage", "semoir"],
    C: ["accord", "refrain", "solfège", "mandoline"],
}
NOISE = [
    "bonjour",
    "demain",
    "voiture",
    "ciel",
    "lundi",
    "train",
    "livre",
    "pluie",
    "vélo",
    "lampe",
]
BASE = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
GRAPPES_YAML = {
    A: ("Cuisine", ["web:cuisine-demo.test", "x:chef_demo"], [B]),
    B: ("Jardinage", ["x:jardin_demo"], [A]),
    C: ("Musique", ["web:musique-demo.test"], []),
}


class Corpus:
    def __init__(self, seed: int):
        self.rnd = random.Random(seed)
        self.rx = random.Random(
            seed + 1
        )  # flux séparé : le vocabulaire EXTRA ne décale aucun autre tirage
        self.web: list[dict] = []
        self.x: dict[str, dict] = {}  # handle -> {name, tweets}
        self.tg: dict[str, dict] = {}  # clé -> {name, id, messages}
        self.ig: list[dict] = []
        self.n = 0
        self.attendu: dict = {"entites": {}, "resultat": {}, "hors_resultat": []}

    # --- texte et dates ----------------------------------------------------------------
    def themed(self, g: str, k: int = 4) -> str:
        words = self.rnd.sample(TERMS[g], k=min(k, len(TERMS[g]))) + self.rnd.choices(NOISE, k=3)
        self.rnd.shuffle(words)
        return " ".join(words + self.rx.sample(EXTRA[g], 2))

    def extra_only(self, g: str) -> str:
        return " ".join(self.rx.sample(EXTRA[g], 3) + self.rx.choices(NOISE, k=3))

    def neutral(self) -> str:
        return " ".join(self.rnd.choices(NOISE, k=6))

    def when(self, old: bool = False) -> datetime:
        days = 400 + self.rnd.randrange(30) if old else self.rnd.randrange(0, 60)
        return BASE - timedelta(days=days, minutes=self.rnd.randrange(1440))

    # --- écosystèmes ------------------------------------------------------------------
    def page(self, domain, site_name, text, links=(), old=False, title=""):
        self.n += 1
        ts = self.when(old).isoformat().replace("+00:00", "Z")
        url = f"https://{domain}/p{self.n}"
        self.web.append(
            {
                "url": f"{url}?utm_source=x", "canonical_url": url, "title": title or "Page",
                "text": text, "lang": "fr", "published_at": ts, "fetched_at": ts,
                "site_name": site_name,
                "links": [{"href": h, "anchor": "lien"} for h in links],
            }
        )  # fmt: skip

    def tweet(self, handle, name, text, mentions=(), urls=(), old=False):
        self.n += 1
        acc = self.x.setdefault(handle, {"name": name, "tweets": []})
        ent: dict = {}
        if mentions:
            ent["mentions"] = [{"username": m, "id": f"id_{m}"} for m in mentions]
        if urls:
            ent["urls"] = [{"url": f"https://t.co/{self.n}", "expanded_url": u} for u in urls]
        body = text + "".join(f" @{m}" for m in mentions)
        acc["tweets"].append(
            {"id": str(10**18 + self.n), "text": body, "author_id": f"id_{handle}",
             "created_at": self.when(old).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "lang": "fr",
             "entities": ent}
        )  # fmt: skip

    def message(self, key, name, cid, text, links=(), forwarded=None, old=False):
        self.n += 1
        ch = self.tg.setdefault(key, {"name": name, "id": cid, "messages": []})
        ts = self.when(old)
        msg = {
            "id": len(ch["messages"]) + 2, "type": "message",
            "date": ts.strftime("%Y-%m-%dT%H:%M:%S"), "date_unixtime": str(int(ts.timestamp())),
            "from": name, "text": text,
            "text_entities": [{"type": "plain", "text": text}]
            + [{"type": "link", "text": u} for u in links],
        }  # fmt: skip
        if links:
            msg["text"] = text + " " + " ".join(links)
        if forwarded:
            msg["forwarded_from"] = forwarded
        ch["messages"].append(msg)

    def insta(self, user, caption):
        self.n += 1
        self.ig.append(
            {"id": f"179{self.n:014d}", "media_type": "IMAGE", "caption": caption, "username": user,
             "permalink": f"https://www.instagram.com/p/DEMO{self.n}/", "like_count": 3,
             "comments_count": 0, "timestamp": self.when().strftime("%Y-%m-%dT%H:%M:%S+0000")}
        )  # fmt: skip


def build(seed: int = 11) -> Corpus:
    c = Corpus(seed)
    rnd = c.rnd
    labels = {A: "Cuisine", B: "Potager", C: "Musique"}
    low = {A: "cuisine", B: "potager", C: "musique"}
    sites = {
        g: [
            (f"{low[g]}-info-{i}.test", f"{labels[g]} Info {i}")
            for i in range(1, 7 if g != C else 6)
        ]
        for g in (A, B, C)
    }
    sites[A][0] = ("cuisine-demo.test", "Cuisine Demo")  # ancre
    sites[C][0] = ("musique-demo.test", "Musique Demo")  # ancre
    accounts = {  # (identifiant, nom, institutionnel ?)
        A: [("chef_demo", "Atelier Chef Demo", True), ("alice_cuisine", "Alice Démo", False), ("journal_cuisine", "Journal Cuisine Demo", True), ("bob_four", "Bob Démo", False)],
        B: [("jardin_demo", "Association Jardin Demo", True), ("clara_semis", "Clara Démo", False), ("radio_potager", "Radio Potager Demo", True)],
        C: [("orchestre_demo", "Orchestre Demo Officiel", True), ("denis_guitare", "Denis Démo", False)],
    }  # fmt: skip
    channels = {  # clé -> (nom affiché, id natif, identifiant public connu ou None)
        A: [("c1", "Canal Cuisine Un", 1001, "canal_cuisine_un"), ("c2", "Café Crème Actu", 1002, None), ("c3", "Chaîne Recettes", 1003, None)],
        B: [("j1", "Canal Jardin", 2001, None), ("j2", "Canal Jardin", 2002, None), ("j3", "Canal Potager Trois", 2003, "canal_potager_trois")],
        C: [("m1", "Canal Musique Un", 3001, "canal_musique_un"), ("m2", "Scène Ouverte", 3002, None)],
    }  # fmt: skip
    public = {"c1": "canal_cuisine_un", "c2": "cafe_creme_actu", "c3": "chaine_recettes", "j1": "canal_jardin",
              "j2": "canal_jardin", "j3": "canal_potager_trois", "m1": "canal_musique_un", "m2": "scene_ouverte"}  # fmt: skip

    def links_to_members(g, k=2):
        out = []
        for _ in range(k):
            kind = rnd.choice(["web", "tg", "x"])
            if kind == "web":
                out.append(f"https://{rnd.choice(sites[g])[0]}/")
            elif kind == "tg":
                out.append(f"https://t.me/{public[rnd.choice([ch[0] for ch in channels[g]])]}")
            else:
                out.append(f"https://x.com/{rnd.choice(accounts[g])[0]}")
        return out

    # --- membres solides ------------------------------------------------------------------
    for g in (A, B, C):
        for dom, name in sites[g]:
            for _ in range(14):
                c.page(
                    dom,
                    name,
                    c.themed(g),
                    links=links_to_members(g, rnd.randrange(0, 3))
                    + rnd.choice(
                        [
                            [],
                            ["https://t.me/+InvitationPrivee"],
                            ["https://www.instagram.com/p/DEMOXYZ/"],
                            ["https://x.com/i/web/status/1"],
                        ]
                    ),
                )
        for handle, name, _inst in accounts[g]:
            for _ in range(10):
                c.tweet(
                    handle,
                    name,
                    c.themed(g),
                    urls=[l for l in links_to_members(g, 1) if "x.com" not in l],
                )
        for key, name, cid, _pub in channels[g]:
            for _ in range(12):
                c.message(
                    key,
                    name,
                    cid,
                    c.themed(g),
                    links=[l for l in links_to_members(g, 1) if "t.me" in l or ".test" in l],
                )
    for i in range(3):
        c.insta(
            f"insta_cuisine_{i}",
            f"{c.themed(A)} https://cuisine-info-{i + 1}.test/ @insta_cuisine_{(i + 1) % 3}",
        )

    # --- cibles de recoupement (texte propre neutre) -------------------------------------
    att = c.attendu["entites"]

    def cible(domain, name, expected, eco="web"):
        for _ in range(3):
            if eco == "web":
                c.page(domain, name, c.neutral())
        att[f"web:{domain}"] = expected

    def cited_by(g, target_url, k):  # k citants de la grappe g : web, X, Telegram, par tours
        pool = (
            [("web", s) for s in sites[g]]
            + [("x", a) for a in accounts[g]]
            + [("tg", ch) for ch in channels[g]]
        )
        for kind, who in pool[:k]:
            if kind == "web":
                c.page(who[0], who[1], c.neutral(), links=[target_url])
            elif kind == "x":
                c.tweet(who[0], who[1], c.neutral(), urls=[target_url])
            else:
                c.message(who[0], who[1], who[2], c.neutral(), links=[target_url])

    for g, dom, name in (
        (A, "cible-valide-a.test", "Cible Validée Cuisine"),
        (B, "cible-valide-b.test", "Cible Validée Jardin"),
        (C, "cible-valide-c.test", "Cible Validée Musique"),
    ):
        cible(dom, name, {"grappe": g, "via": "recoupement"})
        cited_by(g, f"https://{dom}/", 7)
    cible("cible-peu-citee.test", "Cible Peu Citée", {"refus": "citants"})
    cited_by(A, "https://cible-peu-citee.test/", 2)
    cible("cible-hors-fenetre.test", "Cible Hors Fenêtre", {"refus": "fenetre"})
    for who in (sites[A][1], sites[A][2], sites[A][3]):
        c.page(who[0], who[1], c.neutral(), links=["https://cible-hors-fenetre.test/"], old=True)
    for who in (accounts[A][0], accounts[A][1]):
        c.tweet(who[0], who[1], c.neutral(), urls=["https://cible-hors-fenetre.test/"], old=True)
    # mono-écosystème : 5 sites web seulement citent la cible (un seul écosystème)
    cible("cible-mono-eco.test", "Cible Mono Écosystème", {"refus": "ecosysteme"})
    for dom, name in sites[A][:5]:
        c.page(dom, name, c.neutral(), links=["https://cible-mono-eco.test/"])
    # citants non membres : 6 sources neutres de 3 écosystèmes
    cible("cible-bruit.test", "Cible Bruit", {"refus": "non_membres"})
    for i in range(6):
        link = "https://cible-bruit.test/"
        (
            c.page(f"bruit-{i}.test", f"Bruit {i}", c.neutral(), links=[link])
            if i % 3 == 0
            else c.tweet(f"bruit_{i}", f"Bruit {i}", c.neutral(), urls=[link])
            if i % 3 == 1
            else c.message(f"b{i}", f"Canal Bruit {i}", 9000 + i, c.neutral(), links=[link])
        )
    # cercle fermé de sources neutres qui se citent (mono-écosystème, non membres)
    ring = [(f"cercle-{i}.test", f"Cercle {i}") for i in range(5)]
    for i, (dom, name) in enumerate(ring):
        for _ in range(3):
            c.page(dom, name, c.neutral(), links=[f"https://{ring[(i + 1) % 5][0]}/"])
        att[f"web:{dom}"] = {"refus": "cercle"}

    # --- ambiguïtés, oppositions, chevauchements -------------------------------------------
    for _ in range(8):  # opposition : cuisine ET jardinage
        c.page(
            "mixte-ab.test",
            "Mixte Cuisine Jardin",
            " ".join(rnd.sample(TERMS[A][:4], 3) + rnd.sample(TERMS[B][:4], 3)),
        )
    att["web:mixte-ab.test"] = {"statut": "ambigu"}
    for _ in range(8):  # pas d'opposition : seule la marge décide (jardinage et musique à égalité)
        c.page(
            "mixte-bc.test",
            "Mixte Jardin Musique",
            " ".join(rnd.sample(TERMS[B][:4], 3) + rnd.sample(TERMS[C][:4], 3)),
        )
    att["web:mixte-bc.test"] = {"statut": "ambigu"}
    for _ in range(8):  # chevauchement de lexique : « marché » compte pour A et pour B
        c.page("marche-info.test", "Marché Info", "marché marché marché " + c.neutral())
    att["web:marche-info.test"] = {"statut": "ambigu_ou_faible"}
    for _ in range(6):  # sources sans rapport
        c.page("sans-rapport.test", "Sans Rapport", c.neutral())
    att["web:sans-rapport.test"] = {"statut": "non_classe"}

    # --- entités « enrichies » : confirmées par recoupement, texte au vocabulaire du thème (EXTRA) ------
    a0 = c.attendu
    a0["enrichies"], a0["cachees"] = {}, {}
    for g in (A, B, C):
        a0["enrichies"][g] = []
        for i in range(1, 4):
            dom, nom = f"cible-enrichie-{low[g]}-{i}.test", f"Cible Enrichie {labels[g]} {i}"
            for _ in range(3):
                c.page(dom, nom, c.extra_only(g))
            cited_by(g, f"https://{dom}/", 7)
            att[f"web:{dom}"] = {"grappe": g, "via": "recoupement"}
            a0["enrichies"][g].append(nom)
        # entités « cachées » : seulement le vocabulaire EXTRA, jamais citées, donc non classées au départ
        a0["cachees"][g] = []
        for i in range(1, 3):
            dom, nom = f"cachee-{low[g]}-{i}.test", f"Découverte {labels[g]} {i}"
            for _ in range(4):
                c.page(dom, nom, c.extra_only(g))
            a0["cachees"][g].append(f"web:{dom}")

    # --- noms de personnes et comptes sans nom --------------------------------------------
    c.insta("insta_sans_nom", c.themed(B))  # Instagram ne donne aucun nom lisible
    # --- ce que doit produire la chaîne complète (intention de conception) ---------------------
    a = c.attendu
    a["resultat"] = {
        A: ["Cible Enrichie Cuisine 1", "Cible Enrichie Cuisine 2", "Cible Enrichie Cuisine 3", "Café Crème Actu", "Canal Cuisine Un", "Chaîne Recettes", "Cible Validée Cuisine", "Cuisine Demo",
            *[f"Cuisine Info {i}" for i in range(2, 7)], "Journal Cuisine Demo"],
        B: ["Cible Enrichie Potager 1", "Cible Enrichie Potager 2", "Cible Enrichie Potager 3", "Association Jardin Demo", "Canal Jardin", "Canal Potager Trois", "Cible Validée Jardin",
            *[f"Potager Info {i}" for i in range(1, 7)], "Radio Potager Demo"],
        C: ["Cible Enrichie Musique 1", "Cible Enrichie Musique 2", "Cible Enrichie Musique 3", "Canal Musique Un", "Cible Validée Musique", "Musique Demo",
            *[f"Musique Info {i}" for i in range(2, 6)], "Orchestre Demo Officiel", "Scène Ouverte"],
    }  # fmt: skip
    a["hors_resultat"] = [
        "Alice Démo", "Bob Démo", "Clara Démo", "Denis Démo",  # personnes physiques
        "Atelier Chef Demo",  # aucun indice institutionnel explicite : en cas de doute, une personne
        "Cible Peu Citée", "Cible Hors Fenêtre", "Cible Mono Écosystème", "Cible Bruit",  # recoupements refusés
        "Mixte Cuisine Jardin", "Mixte Jardin Musique", "Marché Info",  # ambigus ou faibles
        "Sans Rapport", *[f"Cercle {i}" for i in range(5)],
        *[f"Découverte {n} {i}" for n in ("Cuisine", "Potager", "Musique") for i in (1, 2)],  # non classées au départ
    ]  # fmt: skip
    a["canaux"] = {
        "homonymes": ["telegram:canal_jardin", "telegram:canal_jardin~2002"],
        "hypothese": ["telegram:cafe_creme_actu", "telegram:chaine_recettes", "telegram:canal_jardin"],
        "exacts": ["telegram:canal_cuisine_un", "telegram:canal_potager_trois", "telegram:canal_musique_un"],
    }  # fmt: skip
    return c


SPAM = ["promoxyz", "gratuitement", "bonusmirage", "cliquezici"]


def ajouter_cercle_piege(out: Path) -> dict:
    """Corpus piégé : un cercle de fausses sources (web et X) qui bourrent le lexique de la grappe
    cuisine, se citent entre elles et « confirment » de fausses cibles bourrées de mots de spam.
    Aucune ancre ni source trouvée par la recherche ne les cite : elles ne doivent rien enseigner."""
    rnd = random.Random(99)
    web = []
    domaines = [f"fausse-{i}.test" for i in range(6)]
    cibles = [f"fausse-cible-{j}.test" for j in range(3)]
    bourrage = " ".join(TERMS[A][:4])

    def page(dom, titre, texte, liens):
        return {
            "url": f"https://{dom}/p", "canonical_url": f"https://{dom}/p", "title": "Page",
            "text": texte, "site_name": titre, "published_at": "2026-09-20T10:00:00Z",
            "links": [{"href": f"https://{h}/", "anchor": "lien"} for h in liens],
        }  # fmt: skip

    for i, dom in enumerate(domaines):
        pairs = [d for d in domaines if d != dom]
        for k in range(4):
            web.append(
                page(
                    dom,
                    f"Fausse Source {i}",
                    f"{bourrage} {' '.join(rnd.sample(SPAM, 3))}",
                    rnd.sample(pairs, 2) + cibles,
                )
            )
    for j, dom in enumerate(cibles):
        for _ in range(3):
            web.append(
                page(dom, f"Fausse Cible {j}", " ".join(rnd.choices(SPAM, k=5)) + " bonjour", [])
            )
    for i in range(3):
        handle = f"faux_x_{i}"
        tw = [
            {"id": str(9 * 10**18 + i * 10 + k), "text": f"{bourrage} {' '.join(rnd.sample(SPAM, 3))}", "author_id": f"id_{handle}",
             "created_at": "2026-09-20T10:00:00.000Z", "entities": {"urls": [{"url": f"https://t.co/{k}", "expanded_url": f"https://{cibles[k % 3]}/"} for k in range(3)]}}
            for k in range(4)
        ]  # fmt: skip
        doc = {
            "data": tw,
            "includes": {
                "users": [{"id": f"id_{handle}", "username": handle, "name": f"Faux Compte {i}"}]
            },
        }
        (out / "x" / f"x_{handle}.json").write_text(json.dumps(doc, ensure_ascii=False), "utf-8")
    with (out / "web" / "piege.jsonl").open("w", encoding="utf-8") as f:
        for r in web:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"spam": SPAM, "cibles": cibles, "sources": domaines}


def write(out: Path, seed: int = 11) -> dict:
    c = build(seed)
    if out.exists():
        shutil.rmtree(out)
    for sub in ("x", "telegram", "instagram", "web", "config/grappes"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    for handle, acc in sorted(c.x.items()):
        users = [{"id": f"id_{handle}", "username": handle, "name": acc["name"]}]
        users += [
            {"id": f"id_{m}", "username": m, "name": c.x[m]["name"]}
            for m in sorted(c.x)
            if m != handle
        ][:0]
        doc = {
            "data": acc["tweets"],
            "includes": {"users": users},
            "meta": {"result_count": len(acc["tweets"])},
        }
        # les mentions portent username et id : résolues sans includes supplémentaire
        (out / "x" / f"x_{handle}.json").write_text(json.dumps(doc, ensure_ascii=False), "utf-8")
    for key, ch in sorted(c.tg.items()):
        msgs = [
            {
                "id": 1,
                "type": "service",
                "action": "create_channel",
                "date": "2026-01-01T00:00:00",
                "date_unixtime": "1767225600",
            },
            *ch["messages"],
        ]
        doc = {"name": ch["name"], "type": "public_channel", "id": ch["id"], "messages": msgs}
        (out / "telegram" / f"telegram_{key}.json").write_text(
            json.dumps(doc, ensure_ascii=False), "utf-8"
        )
    (out / "instagram" / "instagram_1.json").write_text(
        json.dumps(
            {"data": c.ig, "paging": {"cursors": {"before": "B", "after": "A"}}}, ensure_ascii=False
        ),
        "utf-8",
    )
    with (out / "web" / "web.jsonl").open("w", encoding="utf-8") as f:
        for r in c.web:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # configuration : 3 grappes, table Telegram, scoring du dépôt
    root = Path(__file__).resolve().parents[1] / "config"
    shutil.copy(root / "scoring.yaml", out / "config" / "scoring.yaml")
    for gid, (label, anchors, opp) in GRAPPES_YAML.items():
        lines = [f"id: {gid}", f'label: "{label}"', "lexicon:", "  terms:"]
        lines += [f'    "{t}": 1.0' for t in TERMS[gid]]
        lines += ["anchors:"] + [f'  - "{a}"' for a in anchors]
        if opp:
            lines += ["oppositions:"] + [f'  - "{o}"' for o in opp]
        (out / "config" / "grappes" / f"{gid.lower()}.yaml").write_text(
            "\n".join(lines) + "\n", "utf-8"
        )
    (out / "config" / "telegram.yaml").write_text(
        "equivalences:\n"
        + "".join(
            f'  "{n}": {p}\n'
            for k, n, i, p in [
                ("c1", "Canal Cuisine Un", 1, "canal_cuisine_un"),
                ("j3", "Canal Potager Trois", 3, "canal_potager_trois"),
                ("m1", "Canal Musique Un", 4, "canal_musique_un"),
            ]
        ),
        encoding="utf-8",
    )
    c.attendu["n_observations_approx"] = (
        len(c.web)
        + sum(len(a["tweets"]) for a in c.x.values())
        + sum(len(h["messages"]) for h in c.tg.values())
        + len(c.ig)
    )
    (out / "attendu.json").write_text(
        json.dumps(c.attendu, ensure_ascii=False, indent=2, sort_keys=True), "utf-8"
    )
    return c.attendu


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    att = write(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 11)
    print("corpus écrit dans", sys.argv[1], "—", att["n_observations_approx"], "observations")
