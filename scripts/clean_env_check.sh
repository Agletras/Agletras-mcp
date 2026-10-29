#!/usr/bin/env bash
# Clean-environment verification for agletras-mcp (Phase 7 + Phase 8).
#
# Runs entirely offline against the repository checkout:
#   1. the documented example TOML config validates,
#   2. `agletras doctor` passes every required check in a throwaway home,
#   3. the offline demo completes a full research run,
#   3b. the Phase 8 monitor workflow works model-free (add -> check -> change -> report),
#   4. the full unit/protocol suite passes (hermetic: temp homes + local HTTP).
#
# Usage:  scripts/clean_env_check.sh [--skip-tests]
# Exit:   0 all checks passed, 1 any check failed.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "clean-env: $PYTHON not found; create .venv first (see README Setup)" >&2
  exit 1
fi

TMP_HOME="$(mktemp -d /tmp/opencode/agletras-clean-XXXX)"
trap 'rm -rf "$TMP_HOME"' EXIT

failures=0
step() { printf '\n== %s\n' "$1"; }
ok()   { printf '   ok: %s\n' "$1"; }
bad()  { printf '   FAILED: %s\n' "$1"; failures=$((failures + 1)); }

step "1. example configuration validates"
if out="$("$PYTHON" -m agletras_mcp.cli config validate --config config.example.toml --json)"; then
  if grep -q '"ok": true' <<<"$out"; then
    ok "config.example.toml validates"
  else
    bad "config validate reported errors: $out"
  fi
else
  bad "config validate exited non-zero"
fi

step "2. doctor: required checks in a throwaway home"
if out="$(AGLETRAS_HOME="$TMP_HOME" "$PYTHON" -m agletras_mcp.cli doctor --skip-mcp --json -q)"; then
  if grep -q '"ok": true' <<<"$out"; then
    ok "doctor reports ok (required checks pass)"
  else
    bad "doctor reported problems: $out"
  fi
else
  bad "doctor exited non-zero"
fi

step "3. offline demo run (plan -> report)"
if "$PYTHON" scripts/demo_research.py --json >/dev/null 2>"$TMP_HOME/demo.err"; then
  ok "demo completed a research run"
else
  bad "demo failed (see $TMP_HOME/demo.err)"
fi

step "3b. monitor workflow (add -> check -> change -> report)"
MONITORED="$TMP_HOME/monitored.txt"
printf 'Baseline line for change detection.\n' >"$MONITORED"
monitor_cmd() { AGLETRAS_HOME="$TMP_HOME" AGLETRAS_LOCAL_ROOTS="$TMP_HOME" "$PYTHON" -m agletras_mcp.cli monitor "$@" --json -q; }
if out="$(monitor_cmd add "$MONITORED")" && grep -q '"created": true' <<<"$out"; then
  ok "monitoring target added"
else
  bad "monitor add failed: $out"
fi
if out="$(monitor_cmd check)" && grep -q '"initial"' <<<"$out"; then
  ok "baseline check stored"
else
  bad "monitor check failed: $out"
fi
printf 'Baseline line for change detection.\nA new substantive line.\n' >"$MONITORED"
if out="$(monitor_cmd check)" && grep -q '"changed"' <<<"$out"; then
  ok "substantive change detected"
else
  bad "second monitor check failed: $out"
fi
if out="$(monitor_cmd report)" && grep -q '"created": true' <<<"$out"; then
  ok "local monitoring report written"
else
  bad "monitor report failed: $out"
fi

if [[ "${1:-}" != "--skip-tests" ]]; then
  step "4. full test suite (hermetic)"
  if AGLETRAS_COLLECT_BACKOFF=0 "$PYTHON" -m unittest discover -s tests -t . -q >"$TMP_HOME/tests.out" 2>&1; then
    ok "unittest suite passed ($(grep -c '^' "$TMP_HOME/tests.out" || true) output lines)"
  else
    bad "test suite failed; tail of output:"
    tail -20 "$TMP_HOME/tests.out" | sed 's/^/     /'
  fi
else
  ok "test suite skipped (--skip-tests)"
fi

printf '\n'
if [[ "$failures" -eq 0 ]]; then
  echo "clean-env check: ALL CHECKS PASSED"
  exit 0
fi
echo "clean-env check: $failures CHECK(S) FAILED" >&2
exit 1
