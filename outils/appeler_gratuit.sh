#!/bin/bash
# Journalise chaque appel au compte gratuit (sednal-free / CCR) puis l'exécute, et note son issue.
# Usage : outils/appeler_gratuit.sh "<description courte>" -- <commande complète>
# Le gateway CCR doit tourner (port 3456) : sudo -iu sednal-free bash -lc 'ccr start --daemon --no-open'
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DESCRIPTION="$1"
shift
shift  # retire le séparateur --
mkdir -p "$ROOT/donnees"
echo "$(date -Iseconds) | $DESCRIPTION" >> "$ROOT/donnees/.journal_ccr.log"
"$@"
rc=$?
echo "$(date -Iseconds) |   -> code de sortie $rc" >> "$ROOT/donnees/.journal_ccr.log"
exit $rc
