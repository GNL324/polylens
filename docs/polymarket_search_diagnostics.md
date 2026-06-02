# Polymarket Search Diagnostics

Gamma `/markets` remote filters are useful but not sufficient for live scanning. The listing endpoint supports market filters such as `active` and category/tag-style filters, while full-text search is exposed separately through Gamma search/public-search parameters such as `q`/`query` and event tag filters. Because `/markets?q=...&tag_slug=...` can still return unrelated rows, Polylens applies local filtering after every live Polymarket discovery call.

## Debug Command

```bash
python -m src.cli debug-polymarket-search --keyword knicks --sport basketball_nba --limit 50
python -m src.cli debug-polymarket-search --keyword knicks --sport basketball_nba --json
```

The output shows raw markets returned, filtered markets retained, discarded markets, and discard reasons.

## scan-live-arb Summary

`scan-live-arb --json` includes:

```json
{
  "polymarket_raw_markets": 100,
  "polymarket_filtered_markets": 12,
  "polymarket_discarded_markets": 88
}
```

Markets that fail local keyword, sport, or category checks are discarded before Kalshi/sportsbook matching.
