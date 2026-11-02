#!/bin/bash
# Hook Stop (réglage global de Claude Code) : relance l'agent (exit 2) tant qu'une phase de grappes est en cours ; chemins relatifs au projet.
# Chaque relance fait relire TOUT le contexte de la session : on ne relance que lorsque c'est utile (2026-10-20).
#  1. une seule relance par arrêt : si l'agent continue déjà à cause de ce hook (stop_hook_active), on le laisse s'arrêter ;
#  2. seulement pour une session ouverte dans grappes ou pilote (le réglage est global : les autres projets ne sont pas concernés) ;
#  3. rien à relancer si ETAT.md dit « Phase en cours : aucune » (ligne tenue à jour en tête du fichier) ;
#  4. anti-boucle d'origine : au plus deux relances sans nouveau commit.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENTREE="$(cat 2>/dev/null || true)"
if command -v jq >/dev/null 2>&1 && [ -n "$ENTREE" ]; then
  [ "$(printf '%s' "$ENTREE" | jq -r '.stop_hook_active // false' 2>/dev/null)" = "true" ] && exit 0
  CWD="$(printf '%s' "$ENTREE" | jq -r '.cwd // empty' 2>/dev/null)"
  case "$CWD" in "" | /opt/grappes* | /opt/pilote*) ;; *) exit 0 ;; esac
fi
cd "$ROOT" || exit 0
grep -q '^Phase en cours : aucune' "$ROOT/ETAT.md" 2>/dev/null && exit 0
mkdir -p "$ROOT/donnees"
ETAT_FILE="$ROOT/donnees/.stop_hook_state"
HEAD=$(git rev-parse HEAD 2>/dev/null || echo "none")
if [ -f "$ETAT_FILE" ]; then read -r LAST_HEAD COUNT < "$ETAT_FILE"; else LAST_HEAD=""; COUNT=0; fi
if [ "$HEAD" = "$LAST_HEAD" ]; then COUNT=$((COUNT+1)); else COUNT=0; fi
echo "$HEAD $COUNT" > "$ETAT_FILE"
[ "$COUNT" -ge 2 ] && exit 0
echo "Relis ETAT.md et ne traite que la phase en cours. Si un point est reellement bloquant (bug, decision irreversible), decris-le clairement et arrete-toi sur ce point precis." >&2
exit 2
