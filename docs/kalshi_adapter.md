# Kalshi Adapter

Polylens uses Kalshi public market data to look for conservative overlap between markets a wallet traded on Polymarket and currently available Kalshi markets.

## Commands

```bash
source /home/noel/.venv/bin/activate
python -m src.cli compare-kalshi <wallet>
python -m src.cli export-wallet <wallet> --include-kalshi
```

`analyze-wallet` keeps its existing behavior and does not fetch Kalshi data by default.

## Data Source

The adapter calls Kalshi's unauthenticated production market-data endpoint:

```text
GET https://external-api.kalshi.com/trade-api/v2/markets?status=open&limit=1000
```

Raw pages are written to `data/raw/kalshi_markets_page*_raw.json`. A simplified cache is written to `data/raw/kalshi_markets_cache.json`. If the public endpoint is unavailable, the adapter can fall back to that cache.

## Matching Approach

The comparison layer normalizes Polymarket titles/slugs/outcomes and Kalshi titles/subtitles/rules into lowercase tokens, extracts simple entities, guesses broad categories, and scores candidate pairs using title similarity plus shared keyword/entity overlap.

The matcher is intentionally conservative. It rejects cross-category pairs, mismatched sports leagues, and weak keyword-only matches. Candidate output includes a reason string so downstream users can review why a match was proposed.

## Limitations

- This is overlap detection, not trade execution or guaranteed arbitrage.
- Kalshi wallet/user data is not included; only public market listings are used.
- Prices and order books are not compared yet.
- Category and league guesses are heuristic and should be reviewed before trading decisions.
- Polymarket resolved/archived markets may not have an active Kalshi equivalent in the current market list.
