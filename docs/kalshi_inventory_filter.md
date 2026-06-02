# Kalshi Inventory Filter

Polylens filters live Kalshi inventory before live cross-venue matching. This keeps single-leg markets that can plausibly match Polymarket or sportsbook markets and removes noisy bundle products before pairwise diagnostics and pricing.

## Kept Product Types

- outright
- championship_winner
- game_winner
- spread
- total

Recognizable single-leg crypto contracts such as KXBTC/KXETH/KXSOL are treated as keepable outright-style binary markets so crypto arbitrage scans remain available.

## Rejected Product Types

- multileg
- cross_category
- player_prop_bundle
- sportsbook_style_same_game_parlay
- unknown

Known noisy prefixes such as `KXMVESPORTSMULTIGAMEEXTENDED` and `KXMVECROSSCATEGORY` are discarded before matching.

## Debug Command

```bash
python -m src.cli debug-kalshi-inventory --limit 100
python -m src.cli debug-kalshi-inventory --limit 100 --json
```

The JSON output includes fetched, retained, and discarded counts plus retained and discarded samples with discard reasons.

## Live Scan Diagnostics

`scan-live-arb --json` includes:

```json
{
  "kalshi_markets_fetched": 100,
  "kalshi_markets_retained": 42,
  "kalshi_markets_discarded": 58,
  "kalshi_inventory_discarded_reason_counts": {
    "rejected sportsbook_style_same_game_parlay": 31,
    "rejected cross_category": 12
  }
}
```

`explain-live-matches --json` reports the Kalshi inventory filter separately under `kalshi_inventory_filter`, so discarded inventory is not confused with matcher rejections.
