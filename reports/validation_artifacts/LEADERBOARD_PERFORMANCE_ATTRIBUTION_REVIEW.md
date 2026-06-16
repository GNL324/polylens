# Leaderboard Performance Attribution Review

Date: 2026-06-16

## Branch and Commit

- Branch under review: feature/leaderboard-performance-attribution
- Review branch: review-leaderboard-performance-attribution
- Reviewed HEAD: f7c76a0
- Base: main at 2db838b
- Review worktree: /home/noel/polylens-review-leaderboard-performance-attribution
- Note: /home/noel/polylens had pre-existing uncommitted changes, so validation used an isolated clean worktree.

## Diff Summary

Diff stat: 13 files changed, 2574 insertions, 13 deletions.

Key changed files: src/intelligence/leaderboard_performance_attribution.py, src/cli.py, src/web/trader_dashboard.py, tests/test_leaderboard_performance_attribution.py, src/intelligence/polymarket_leaderboard_client.py, src/intelligence/polymarket_leaderboard_ingestion.py, tests/test_polymarket_leaderboard.py, docs and report artifacts.

## Files Reviewed

- src/intelligence/leaderboard_performance_attribution.py
- src/intelligence/polymarket_leaderboard_client.py
- src/intelligence/polymarket_leaderboard_ingestion.py
- src/cli.py
- src/web/trader_dashboard.py
- tests/test_leaderboard_performance_attribution.py
- tests/test_polymarket_leaderboard.py

## Security and Safety Review

- Attribution/report paths are analytics-only and do not place orders.
- No private keys, credential writes, or authenticated trading API usage were added by the attribution module.
- Public leaderboard client uses GET against https://data-api.polymarket.com/v1/leaderboard.
- Branch also adds polymarket-leaderboard-fetch and polymarket-leaderboard-ingest, which persist public leaderboard/discovery data. Those are ingestion paths, not the requested report commands.
- Smoke outputs included read_only, paper_only, and analytics_only flags where expected.
- Synthetic wallets were filtered; smoke outputs had synthetic_wallet_count 0 where present and no synthetic fixture wallet appeared.

## Test Results

- python -m pytest tests/test_leaderboard_performance_attribution.py -q: not runnable because python is not on PATH.
- /home/noel/polylens/.venv/bin/python -m pytest tests/test_leaderboard_performance_attribution.py -q: FAILED, 13 passed and 4 failed.
- Failed tests: test_leaderboard_alpha_rankings_cli_flag_registers, test_wallet_performance_breakdown_cli, test_wallet_follow_candidates_cli, test_wallet_strategy_clustering_cli.
- Observed cause: these tests hard-code cwd=/home/noel/polylens, which is a dirty/stale worktree at 2db838b, so subprocesses exercise old CLI code.
- /home/noel/polylens/.venv/bin/python -m pytest tests -q -k  leaderboard or attribution or acquisition or wallet: timed out after 5 minutes; orphaned process terminated after it remained running beyond 6 minutes.
- /home/noel/polylens/.venv/bin/python -m pytest -q: timed out after 5 minutes; orphaned process terminated after it remained running beyond 6 minutes.

## Smoke Command Results

All smoke commands ran from /home/noel/polylens-review-leaderboard-performance-attribution with PYTHONPATH pointing to that worktree.

- python -m src.cli wallet-alpha-rankings --leaderboard-only --json: PASS, valid JSON, safe empty payload, flags read_only/paper_only/analytics_only/leaderboard_only/real_wallet_only present, synthetic_wallet_count 0.
- python -m src.cli wallet-performance-breakdown --json: PASS, valid JSON, safe empty payload, flags read_only/paper_only/analytics_only present, synthetic_wallet_count 0.
- python -m src.cli wallet-follow-candidates --limit 5 --json: PASS, valid JSON, safe empty payload, flags read_only/paper_only/analytics_only present.
- python -m src.cli wallet-strategy-clustering --leaderboard-only --json: PASS, valid JSON, safe empty payload, flags read_only/paper_only/analytics_only/leaderboard_only present, synthetic_wallet_count 0.
- python -m src.cli wallet-strategy-clustering --json: PASS, valid JSON, safe empty payload, flags read_only/paper_only/analytics_only present, leaderboard_only false, synthetic_wallet_count 0.

Persistent data notes: data/trader_discovery.db discovered_wallets count was 0 after smoke commands. data/traders.db and data/trader_discovery.db modification times predated the smoke command run. An untracked data/traders/watchlist.json existed with timestamp 2026-06-16 05:14:57 UTC, predating the smoke command run.

## Dashboard Route Status

- curl -I http://127.0.0.1:8787/acquisition || true: HTTP/1.1 200 OK.
- curl -I http://127.0.0.1:8787/ || true: HTTP/1.1 405 Method Not Allowed with allow GET.
- Follow-up GET for root returned 200 text/html; charset=utf-8.
- No restart was performed. If the service must load this code, run: sudo systemctl restart polylens-dashboard.service.

## Risk Assessment

Risk is medium. The attribution/report implementation appears analytics-only and smoke checks pass, but required tests fail and broader suites timed out. The test hard-coded cwd makes the suite unreliable in isolated review worktrees.

## Final Verdict

BLOCK MERGE

## Test Worktree Isolation Fix

Root cause: tests/test_leaderboard_performance_attribution.py had four CLI subprocess tests hard-coding cwd=/home/noel/polylens and .venv/bin/python. In an isolated review worktree, those subprocesses executed the dirty/stale original checkout instead of the code under review.

Files changed:

- tests/test_leaderboard_performance_attribution.py
- reports/validation_artifacts/LEADERBOARD_PERFORMANCE_ATTRIBUTION_REVIEW.md

Fix summary: the CLI subprocess tests now resolve the repository source root from Path(__file__).resolve().parents[1], call sys.executable with python -m src.cli, set PYTHONPATH to the resolved review worktree, and use pytest tmp_path as subprocess cwd so default data paths stay isolated from production DBs.

Test results after fix:

- Targeted suite: /home/noel/polylens/.venv/bin/python -m pytest tests/test_leaderboard_performance_attribution.py -q -vv passed, 17 passed in 2.03s.
- Scoped suite: /home/noel/polylens/.venv/bin/python -m pytest tests -q -k  leaderboard or attribution or acquisition or wallet passed, 182 passed and 858 deselected in 622.44s, about 10m22s.
- Full suite: /home/noel/polylens/.venv/bin/python -m pytest -q passed, 1040 passed in 524.85s, about 8m45s.

Smoke results after fix:

- python -m src.cli wallet-alpha-rankings --leaderboard-only --json: PASS, valid JSON, read_only/paper_only/analytics_only/leaderboard_only/real_wallet_only present, synthetic fixture absent, synthetic_wallet_count 0.
- python -m src.cli wallet-performance-breakdown --json: PASS, valid JSON, read_only/paper_only/analytics_only present, synthetic fixture absent, synthetic_wallet_count 0.
- python -m src.cli wallet-follow-candidates --limit 5 --json: PASS, valid JSON, read_only/paper_only/analytics_only present, synthetic fixture absent.
- python -m src.cli wallet-strategy-clustering --leaderboard-only --json: PASS, valid JSON, read_only/paper_only/analytics_only/leaderboard_only present, synthetic fixture absent, synthetic_wallet_count 0.
- python -m src.cli wallet-strategy-clustering --json: PASS, valid JSON, read_only/paper_only/analytics_only present, leaderboard_only false, synthetic fixture absent, synthetic_wallet_count 0.

Persistent mutation check: data/traders.db, data/trader_discovery.db, and data/traders/watchlist.json had identical mtime and size before and after the smoke commands.

Final recommendation: READY FOR REVIEW.
