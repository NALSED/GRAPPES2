#!/bin/bash
# Lance un cycle de la boucle automatique (simulation tant que `autonome` est faux dans config/autonomie.yaml).
# Usage : outils/cycle.sh   — appelé par le timer systemd (outils/systemd/), ou à la main.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p donnees rapports
exec "$ROOT/.venv/bin/grappes" cycle >> donnees/cycle.log 2>&1
