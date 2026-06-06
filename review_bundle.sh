#!/usr/bin/env bash
set -u
cd /home/noel/polylens
OUT=/tmp/polylens_review_bundle.txt
{
  echo "===== git status ====="
  git status --short || true
  echo
  echo "===== diff stat ====="
  git diff --stat || true
  echo
  echo "===== full diff ====="
  git diff || true
  echo
  echo "===== tests ====="
  PYTHONPATH=. .venv/bin/pytest -q || true
} > "$OUT"
printf "Wrote %s\n" "$OUT"
