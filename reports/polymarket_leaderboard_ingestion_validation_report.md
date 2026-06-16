# Polymarket Leaderboard Ingestion Validation Report

Branch: `feature/polymarket-leaderboard-ingestion`  
Date: 2026-06-16

## Scope

Read-only, paper-only, analytics-only ingestion of public Polymarket leaderboard wallets into Polylens discovery and acquisition pipelines.

## Deliverables

| # | Deliverable | Status |
|---|---|---|
| 1 | Leaderboard client | `src/intelligence/polymarket_leaderboard_client.py` |
| 2 | Wallet extraction | `extract_leaderboard_wallets()` with synthetic filter |
| 3 | Acquisition integration | `LeaderboardAcquisitionSource` |
| 4 | Registry integration | Via acquisition pipeline + optional registry seed |
| 5 | Discovery integration | `LeaderboardDiscoverySource` |
| 6 | Autonomy integration | `leaderboard_ingestion` cycle |
| 7 | CLI commands | fetch, ingest, health, status |
| 8 | Dashboard visibility | Acquisition page leaderboard section |
| 9 | Full tests | `tests/test_polymarket_leaderboard.py` (12 tests) |
| 10 | Validation report | This document |

## Test Results

```
1023 passed in 441.15s
```

Leaderboard-specific tests cover:

- API payload parsing
- Synthetic wallet rejection (`0xaaaa…`)
- Fetch persistence to SQLite
- Discovery registration
- Acquisition pipeline entry
- Autonomy cycle persistence
- Dashboard health data helpers

## Success Criteria

| Criterion | Result |
|---|---|
| Real wallets automatically discovered | Pass — mock/live leaderboard entries extract `proxyWallet` addresses |
| Synthetic wallets rejected | Pass — known fixtures rejected in extraction and quality filter |
| Leaderboard wallets enter acquisition pipeline | Pass — `polymarket_leaderboard` acquisition source tested |
| Autonomy service runs ingestion safely | Pass — `leaderboard_ingestion` cycle with read-only flags |
| Dashboard shows leaderboard source health | Pass — Acquisition page KPI + recent fetches |
| Full test suite passes | Pass — 1023 tests |

## Safety

- Public `GET /v1/leaderboard` only
- No credentials, execution, or live trading
- All payloads include `read_only`, `paper_only`, `analytics_only` flags

## CLI Verification

```bash
python -m src.cli polymarket-leaderboard-fetch --json
python -m src.cli polymarket-leaderboard-ingest --json
python -m src.cli polymarket-leaderboard-health --json
python -m src.cli polymarket-leaderboard-status --json
```

## Notes

- Default ingestion fetches `OVERALL/MONTH/PNL` and `OVERALL/WEEK/VOL` profiles
- Leaderboard wallets receive rank-based confidence and discovery scores
- Registry profiles are created through the standard acquisition quality filter path
