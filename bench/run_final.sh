#!/usr/bin/env bash
# Final paper measurements (SPEC A4, coverage map approved 2026-10-26), one after another so
# that timing-sensitive runs never share the machine. Each step logs to var/logs/final/<step>.log
# and appends "<step> <exit code> <start> <end>" to var/logs/final/status.txt; a failed step
# does not stop the chain. Phase A: offline (CPU); B: cloud models; C: protocols (quiet
# machine, real time); then the paper export.
#   bench/run_final.sh            # everything
#   bench/run_final.sh B C        # only these phases
set -u
cd "$(dirname "$0")/.."
LOG=var/logs/final
mkdir -p "$LOG"
PG=postgresql://a6g:a6g@127.0.0.1:5433/a6g
PHASES="${*:-A B C}"
export -n PYTHONPATH

step() {  # step <name> <command...>
  local name=$1; shift
  local t0; t0=$(date -Is)
  echo "[$t0] $name: $*" | tee -a "$LOG/status.txt"
  "$@" >"$LOG/$name.log" 2>&1
  local rc=$?
  echo "$name rc=$rc $t0 $(date -Is)" | tee -a "$LOG/status.txt"
}

if [[ " $PHASES " == *" A "* ]]; then
  step n4_scarcity      uv run a6g bench scarcity --jobs 6
  step g10_capacity     uv run a6g bench g10 --seeds 10 --out var/runs/g10_n2 --network capacity --checkpoint-meter \
                          --persistence postgres --pg-dsn "$PG"
  step kpi11_offline    uv run a6g bench kpi11 --seeds 20
  step kpi11_offline_scarcity uv run a6g bench kpi11 --seeds 20 --network capacity
  step n1_outage        uv run a6g bench baselines --jobs 6
  step kb_offline       uv run a6g bench kb
  step decompose_offline uv run a6g bench decompose
  step n2_overhead      uv run a6g bench overhead --persistence postgres
fi
if [[ " $PHASES " == *" B "* ]]; then
  step kpi11_cloud      uv run a6g bench kpi11 --seeds 20 --model --robot-model
  step kpi11_cloud_scarcity uv run a6g bench kpi11 --seeds 20 --model --robot-model --network capacity
  step kb_cloud         uv run a6g bench kb --model --embedder model
  step decompose_cloud  uv run a6g bench decompose --model --embedder model
  for i in 1 2 3; do step "orders_$i" uv run a6g bench orders; done
fi
if [[ " $PHASES " == *" C "* ]]; then
  step n5_protocols     uv run a6g bench protocols
  step n6_export        uv run a6g paper export
fi
