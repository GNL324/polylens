# Leaderboard Performance Attribution Report

## Branch

- **Branch:** `feature/leaderboard-performance-attribution`
- **Base:** `main` at `2db838b`

## Files Changed

| File | Change |
|---|---|
| `src/intelligence/leaderboard_performance_attribution.py` | New read-only analytics module |
| `src/cli.py` | New CLI commands and flags |
| `src/web/trader_dashboard.py` | Leaderboard Attribution dashboard section |
| `tests/test_leaderboard_performance_attribution.py` | 17 tests |
| `reports/leaderboard_performance_attribution_report.md` | This report |

## Commands Added

| Command | Purpose |
|---|---|
| `wallet-alpha-rankings --leaderboard-only` | Alpha rankings filtered to `polymarket_leaderboard` source |
| `wallet-performance-breakdown` | Aggregate stats for leaderboard-derived wallets |
| `wallet-follow-candidates` | Top leaderboard follow candidates with reasons |
| `wallet-strategy-clustering --leaderboard-only` | Lightweight strategy clusters for leaderboard wallets |

## Example JSON Summaries

### wallet-alpha-rankings --leaderboard-only --json (empty state)

```json
{
  "analytics_only": true,
  "leaderboard_only": true,
  "paper_only": true,
  "rankings": [],
  "rankings_count": 0,
  "read_only": true,
  "real_wallet_only": true,
  "synthetic_wallet_count": 0
}
```

### wallet-strategy-clustering --json

```json
{
  "clusters": [
    {
      "average_alpha_score": 64.6069,
      "category": "unknown",
      "top_wallets": [
        {
          "alpha_score": 79.7,
          "confidence": 0.3799,
          "discovery_score": 80,
          "wallet": "0x927f7694de44d19a72bce76254e628d1c141d215"
        }
      ],
      "wallet_count": 3
    }
  ],
  "leaderboard_only": false,
  "read_only": true,
  "synthetic_wallet_count": 0,
  "total_classified": 3
}
```

## Tests Run

| Suite | Result |
|---|---|
| `tests/test_leaderboard_performance_attribution.py` | 17 passed |
| `pytest -q -k "leaderboard or attribution or acquisition or wallet"` | 182 passed, 858 deselected |
| Full suite `pytest -q` | **1040 passed** in 167.77s |

## Safety Review

| Check | Result |
|---|---|
| Live trading | ❌ not touched |
| Order placement | ❌ not added |
| Execution paths | ❌ not added |
| Authenticated APIs | ❌ not used |
| Credential handling | ❌ not added |
| Private keys / secrets | ❌ not added |
| DB writes from attribution | ❌ none (read-only analytics) |
| Synthetic wallets in rankings | ❌ rejected by `wallet_synthetic_filter` |
| Dashboard `/acquisition` route | ✅ still renders |

## Limitations

- All new commands are read-only and depend on prior ingestion / acquisition / alpha data.
- If no leaderboard wallets have been ingested, commands return empty or zeroed payloads gracefully.
- Strategy clustering currently uses lightweight heuristics over existing metadata; labels are `unknown` when no leaderboard metadata table exists.
- The dashboard attribution section catches exceptions per subsection so one unavailable metric does not break the page.

## Next Recommended Follow-up

1. Run the new commands on Predix after a fresh leaderboard ingestion cycle to capture non-zero real-world metrics.
2. Consider persisting lightweight strategy cluster labels if they prove stable over time.
3. Add trend tracking for leaderboard wallet alpha scores once enough cycles accumulate.

---

READY FOR REVIEW
