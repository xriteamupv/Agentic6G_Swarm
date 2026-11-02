#!/usr/bin/env bash
# Collect what exists only on this machine, for the move to the workstation (SPEC A5.2).
# Everything else comes from GitHub (`develop`) or is rebuilt (see docs/WORKSTATION.md).
#
#   scripts/migrate/export_local_state.sh [OUT_DIR] [--with-runs]
#
# OUT_DIR (default ~/a6g-migration) receives:
#   backups.bundle    git bundle of the local-only branches backup/pre-rewrite and
#                     backup/main-before-squash (never pushed to GitHub)
#   secrets/.env      the repository's .env (API keys): keep the folder private, delete it
#                     after the move
#   claude-memory.tgz Claude Code's memory of this project
#   runs.tgz          (--with-runs) the run directories the overhead bench reads (G10 runs
#                     and the 30 outage-sweep runs, about 1.2 GB); every bench result is
#                     committed, so the rest of var/ is not needed
#   MANIFEST.txt      what was exported, from which commit and host
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT="${1:-$HOME/a6g-migration}"
WITH_RUNS=0
for a in "$@"; do [[ "$a" == "--with-runs" ]] && WITH_RUNS=1; done
[[ "$OUT" == "--with-runs" ]] && OUT="$HOME/a6g-migration"
mkdir -p "$OUT/secrets"
chmod 700 "$OUT" "$OUT/secrets"

branches=()
for b in backup/pre-rewrite backup/main-before-squash; do
  git rev-parse --verify --quiet "$b" >/dev/null && branches+=("$b")
done
if ((${#branches[@]})); then
  git bundle create "$OUT/backups.bundle" "${branches[@]}"
fi

if [[ -f .env ]]; then
  install -m 600 .env "$OUT/secrets/.env"
fi

# Claude Code keeps a project's memory under ~/.claude/projects/<working dir with / as ->
MEM="$HOME/.claude/projects/$(dirname "$PWD" | tr '/' '-')/memory"
if [[ -d "$MEM" ]]; then
  tar -czf "$OUT/claude-memory.tgz" -C "$MEM" .
fi

if ((WITH_RUNS)); then
  mapfile -t n1 < <(python3 -c "
import json, glob
f = sorted(glob.glob('bench/results/n2_overhead_*.json'))[-1]
print('\n'.join(json.load(open(f))['inputs']['n1']))")
  tar -czf "$OUT/runs.tgz" var/runs/g10_n2 "${n1[@]}"
fi

{
  echo "exported $(date -Is) from $(hostname) at $(git rev-parse --short HEAD) ($(git branch --show-current))"
  echo "branches in backups.bundle: ${branches[*]:-none}"
  echo "memory from: $MEM"
  ls -la "$OUT" "$OUT/secrets"
} > "$OUT/MANIFEST.txt"
echo "done: $OUT (keep it private: secrets/.env holds API keys)"
