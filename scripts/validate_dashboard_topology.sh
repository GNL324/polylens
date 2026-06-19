#!/usr/bin/env bash
# Read-only Polylens dashboard topology validation.
# Does not restart or modify services.

set -euo pipefail

failures=0

fail() {
  echo "FAIL: $*" >&2
  failures=$((failures + 1))
}

pass() {
  echo "PASS: $*"
}

require_localhost_listener() {
  local port="$1"
  local label="$2"
  local listeners
  local local_addresses
  listeners="$(ss -tlnH "sport = :${port}" 2>/dev/null || true)"

  if [[ -z "${listeners}" ]]; then
    fail "${label} is not listening on port ${port}"
    return
  fi

  local_addresses="$(awk '{print $4}' <<<"${listeners}")"

  if grep -Eq "(^|[[:space:]])(0\.0\.0\.0|\[?::\]?):${port}($|[[:space:]])" <<<"${local_addresses}"; then
    fail "${label} binds 0.0.0.0:${port} (expected 127.0.0.1 only)"
    echo "${listeners}" >&2
    return
  fi

  if ! grep -Eq "(^|[[:space:]])127\.0\.0\.1:${port}($|[[:space:]])" <<<"${local_addresses}"; then
    fail "${label} is not bound to 127.0.0.1:${port}"
    echo "${listeners}" >&2
    return
  fi

  pass "${label} listens on 127.0.0.1:${port} only"
}

http_status() {
  curl -s -o /dev/null -w '%{http_code}' --connect-timeout 3 "$1" 2>/dev/null || echo "000"
}

echo "=== Polylens dashboard topology validation ==="

require_localhost_listener 8787 "Main dashboard"
require_localhost_listener 8788 "Trader dashboard"

if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -E '(0\.0\.0\.0|\[?::\]?):(8787|8788)' >/dev/null; then
  fail "A dashboard is exposed on 0.0.0.0 (8787 or 8788)"
else
  pass "No dashboard listener on 0.0.0.0:8787 or 0.0.0.0:8788"
fi

main_root_status="$(http_status 'http://127.0.0.1:8787/')"
if [[ "${main_root_status}" == "307" ]]; then
  pass "Main dashboard root redirects to /mission-control with HTTP 307 on 8787"
else
  fail "Main dashboard root returned HTTP ${main_root_status} (expected 307 redirect to /mission-control)"
fi

mission_status="$(http_status 'http://127.0.0.1:8787/mission-control')"
if [[ "${mission_status}" == "200" ]]; then
  pass "/mission-control returns HTTP 200 on 8787"
else
  fail "/mission-control returned HTTP ${mission_status} (expected 200)"
fi

trader_status="$(http_status 'http://127.0.0.1:8788/')"
if [[ "${trader_status}" == "200" ]]; then
  pass "Trader dashboard returns HTTP 200 on 8788"
else
  fail "Trader dashboard returned HTTP ${trader_status} (expected 200)"
fi

if command -v systemctl >/dev/null 2>&1; then
  if systemctl show polylens-dashboard.service -p ExecStart --value >/dev/null 2>&1; then
    dashboard_exec="$(systemctl show polylens-dashboard.service -p ExecStart --value 2>/dev/null || true)"
    if grep -q 'web-dashboard --host 127.0.0.1 --port 8787' <<<"${dashboard_exec}"; then
      pass "polylens-dashboard.service ExecStart is correct"
    else
      fail "polylens-dashboard.service ExecStart is not web-dashboard on 127.0.0.1:8787"
      echo "${dashboard_exec}" >&2
    fi
  else
    fail "polylens-dashboard.service is missing or inactive"
  fi

  if systemctl show polylens-trader-dashboard.service -p ExecStart --value >/dev/null 2>&1; then
    trader_exec="$(systemctl show polylens-trader-dashboard.service -p ExecStart --value 2>/dev/null || true)"
    if grep -q 'trader-dashboard --host 127.0.0.1 --port 8788' <<<"${trader_exec}"; then
      pass "polylens-trader-dashboard.service ExecStart is correct"
    else
      fail "polylens-trader-dashboard.service ExecStart is not trader-dashboard on 127.0.0.1:8788"
      echo "${trader_exec}" >&2
    fi
  else
    fail "polylens-trader-dashboard.service is missing or inactive"
  fi
fi

echo "=== Summary: ${failures} failure(s) ==="
if [[ "${failures}" -gt 0 ]]; then
  exit 1
fi

exit 0
