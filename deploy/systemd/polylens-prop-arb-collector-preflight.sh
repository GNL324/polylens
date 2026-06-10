#!/usr/bin/env bash
set -euo pipefail

provider="${POLYLENS_PROP_ARB_COLLECTOR_PROVIDER:-all}"
missing=()

if [[ -z "${ODDS_API_KEY:-}" ]]; then
  missing+=("ODDS_API_KEY")
fi

if [[ "${provider}" == "oddsblaze" || "${provider}" == "all" ]]; then
  if [[ -z "${ODDSBLAZE_API_KEY:-}" ]]; then
    missing+=("ODDSBLAZE_API_KEY")
  fi
fi

if ((${#missing[@]})); then
  echo "polylens-prop-arb-collector: missing required env: ${missing[*]}" >&2
  exit 1
fi

exit 0
