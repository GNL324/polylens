#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <branch>" >&2
  exit 2
fi

BRANCH="$1"

case "$BRANCH" in
  ""|/*|*..*|*//*|*-)
    echo "unsafe branch name: $BRANCH" >&2
    exit 2
    ;;
esac

if [[ "$BRANCH" == -* ]]; then
  echo "unsafe branch name: $BRANCH" >&2
  exit 2
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
WORKTREE_ROOT="/srv/devcloud/worktrees"
WORKTREE_PATH="$WORKTREE_ROOT/$BRANCH"
REPORT_PATH="$REPO_ROOT/reports/ci/$BRANCH.md"
REMOTE="${CI_REMOTE:-origin}"

cd "$REPO_ROOT"

mkdir -p "$(dirname "$WORKTREE_PATH")" "$(dirname "$REPORT_PATH")"

echo "Fetching $BRANCH from $REMOTE"
git fetch --prune "$REMOTE" "$BRANCH"

if [[ -e "$WORKTREE_PATH" ]]; then
  git worktree remove --force "$WORKTREE_PATH"
fi

git worktree add --force --detach "$WORKTREE_PATH" "FETCH_HEAD"

if [[ ! -e "$WORKTREE_PATH/.venv" ]]; then
  ln -s "$REPO_ROOT/.venv" "$WORKTREE_PATH/.venv"
fi

CI_SCRIPT="$WORKTREE_PATH/scripts/dev_ci.sh"
if [[ ! -x "$CI_SCRIPT" ]]; then
  CI_SCRIPT="$REPO_ROOT/scripts/dev_ci.sh"
fi

STATUS="passed"
STARTED_AT="$(date -Is)"
set +e
(
  cd "$WORKTREE_PATH"
  "$CI_SCRIPT"
)
EXIT_CODE=$?
set -e
FINISHED_AT="$(date -Is)"

if [[ "$EXIT_CODE" -ne 0 ]]; then
  STATUS="failed"
fi

{
  echo "# Branch CI Review: $BRANCH"
  echo
  echo "- Remote: $REMOTE"
  echo "- Worktree: $WORKTREE_PATH"
  echo "- Started: $STARTED_AT"
  echo "- Finished: $FINISHED_AT"
  echo "- Status: $STATUS"
  echo "- Exit code: $EXIT_CODE"
  echo
  echo "No merge was performed. No push to main was performed."
} > "$REPORT_PATH"

echo "Report written to $REPORT_PATH"
exit "$EXIT_CODE"
