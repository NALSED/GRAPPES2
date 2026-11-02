#!/usr/bin/env python3
"""Économie de jetons en deux lignes : ccr (compte gratuit) et rtk. Lecture seule, aucune clé, aucun appel réseau.

* ccr : MESURÉ, lu dans la base d'usage du routeur du compte `sednal-free` (~sednal-free/.claude-code-router/app-data/usage.sqlite),
  via `sudo -n -iu sednal-free` (le code est passé sur l'entrée standard : aucun fichier temporaire à partager).
* rtk : ESTIMÉ par rtk (octets / 4), lu commande par commande dans ~/.local/share/rtk/history.db, puis CORRIGÉ : l'entrée de chaque
  commande est plafonnée à ce que Claude Code aurait réellement montré (au-delà d'environ 30 000 caractères la sortie est rangée dans un
  fichier). Sans ce plafond, quelques `ls` sur un dossier de milliers de fichiers comptaient pour 71 % du total « économisé ».

Usage : outils/economies.py [--jour AAAA-MM-JJ] [--csv]   (sans --jour : tout l'historique ; --csv ajoute une ligne à donnees/.economies_jetons.csv)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

PLAFOND_VISIBLE = 7_500  # jetons : ~30 000 caractères, ce que Claude Code laisse entrer dans le contexte avant de ranger la sortie dans un fichier
CSV = Path(__file__).resolve().parent.parent / "donnees" / ".economies_jetons.csv"  # pas .economies_tokens.csv : format différent, et ses lignes « gratuit » sont fausses (0)

CODE_CCR = r"""
import json, os, sqlite3, sys
jour = sys.argv[1] if len(sys.argv) > 1 else ""
f = os.path.expanduser("~/.claude-code-router/app-data/usage.sqlite")
c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
where, args = ("where substr(created_at,1,10) = ?", [jour]) if jour else ("", [])
r = c.execute(f"select count(*), coalesce(sum(status_code = 200),0), coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), "
              f"coalesce(sum(cache_read_tokens),0), coalesce(sum(total_tokens),0) from usage_events {where}", args).fetchone()
print(json.dumps(dict(zip(["requetes", "reussies", "entree", "sortie", "cache", "total"], r))))
"""


def ccr(jour: str) -> dict | None:
    try:
        p = subprocess.run(["sudo", "-n", "-iu", "sednal-free", "python3", "-", jour], input=CODE_CCR, capture_output=True, text=True, timeout=30, check=False)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def rtk(jour: str) -> dict | None:
    f = Path(os.path.expanduser("~/.local/share/rtk/history.db"))
    if not f.exists():
        return None
    c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
    where, args = ("where substr(timestamp,1,10) = ?", [jour]) if jour else ("", [])
    lignes = c.execute(f"select input_tokens, output_tokens, saved_tokens from commands {where}", args).fetchall()
    corrige = sum(max(0, min(i, PLAFOND_VISIBLE) - o) for i, o, _ in lignes)
    vu = sum(min(i, PLAFOND_VISIBLE) for i, _, _ in lignes)
    return {"commandes": len(lignes), "affiche": sum(s for _, _, s in lignes), "corrige": corrige, "vu": vu}


def k(n: float) -> str:
    return f"{n / 1e6:.2f} M".replace(".", ",") if n >= 1e6 else f"{n / 1e3:.1f} K".replace(".", ",") if n >= 1e3 else str(int(n))


def main() -> None:
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("--jour", default="", help="AAAA-MM-JJ (défaut : tout l'historique)")
    a.add_argument("--csv", action="store_true", help="ajoute la ligne du jour à donnees/.economies_jetons.csv")
    o = a.parse_args()
    periode = o.jour or "tout"
    c, r = ccr(o.jour), rtk(o.jour)
    if c is None:
        print(f"ccr ({periode}) : non disponible (base d'usage illisible ou sudo refusé)")
    elif not c["requetes"]:
        print(f"ccr ({periode}) : 0 appel au compte gratuit, 0 jeton déporté")
    else:
        echecs = c["requetes"] - c["reussies"]
        print(f"ccr ({periode}) : {c['requetes']} requêtes au compte gratuit ({echecs} en échec, {100 * echecs / c['requetes']:.0f} %), "
              f"{k(c['total'])} jetons déportés dont {k(c['sortie'])} de sortie utile — mesuré")
    if r is None:
        print(f"rtk ({periode}) : non disponible (historique absent)")
    else:
        pct = 100 * r["corrige"] / r["vu"] if r["vu"] else 0
        print(f"rtk ({periode}) : {k(r['corrige'])} jetons évités sur {r['commandes']} commandes ({pct:.0f} % de ce qui serait entré dans le contexte) — "
              f"estimé, corrigé ; rtk affiche {k(r['affiche'])}")
    if o.csv:
        jour = o.jour or datetime.now().astimezone().date().isoformat()
        nouveau = not CSV.exists()
        with CSV.open("a", encoding="utf-8") as f:
            if nouveau:
                f.write("date,ccr_requetes,ccr_echecs,ccr_jetons,ccr_sortie,rtk_commandes,rtk_evites_corriges,rtk_affiches\n")
            f.write(f"{jour},{(c or {}).get('requetes', '')},{(c['requetes'] - c['reussies']) if c else ''},{(c or {}).get('total', '')},"
                    f"{(c or {}).get('sortie', '')},{(r or {}).get('commandes', '')},{(r or {}).get('corrige', '')},{(r or {}).get('affiche', '')}\n")


if __name__ == "__main__":
    main()
