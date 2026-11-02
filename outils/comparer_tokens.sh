#!/bin/bash
# Compare les jetons Pro (local, sednal) et Gratuit (ccr, sednal-free) pour une date. Lecture seule, aucune clé.
# Le côté gratuit est MESURÉ dans la base d'usage de ccr (outils/economies.py) : l'ancienne version interrogeait
# OpenRouter avec OPENROUTER_MANAGEMENT_KEY, absente, et écrivait donc toujours 0 (corrigé le 2026-10-20).
# Usage : comparer_tokens.sh [AAAA-MM-JJ]
set -euo pipefail

DATE="${1:-$(date +%F)}"
ICI="$(cd "$(dirname "$0")" && pwd)"

# --- Côté Pro (sednal, transcriptions locales de Claude Code) ---
PRO_TOKENS=$(npx -y ccusage@latest daily --json --since "${DATE//-/}" --until "${DATE//-/}" 2>/dev/null \
  | jq '[.daily[]? | (.inputTokens + .outputTokens + .cacheCreationTokens + .cacheReadTokens)] | add // 0')

# --- Côté gratuit (ccr, base d'usage locale) ---
FREE_TOKENS=$(sudo -n -iu sednal-free python3 - "$DATE" <<'PY'
import os, sqlite3, sys
f = os.path.expanduser("~/.claude-code-router/app-data/usage.sqlite")
c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
print(c.execute("select coalesce(sum(total_tokens),0) from usage_events where substr(created_at,1,10) = ?", [sys.argv[1]]).fetchone()[0])
PY
)

TOTAL=$((PRO_TOKENS + FREE_TOKENS))
PCT_FREE="0.0"
[ "$TOTAL" -gt 0 ] && PCT_FREE=$(awk "BEGIN{printf \"%.2f\", ($FREE_TOKENS/$TOTAL)*100}")

echo "=== Répartition des jetons — $DATE ==="
echo "Pro (sednal, cache compris) : $PRO_TOKENS"
echo "Gratuit (sednal-free/ccr)   : $FREE_TOKENS"
echo "Part gratuite               : ${PCT_FREE} %"
"$ICI/economies.py" --jour "$DATE"
