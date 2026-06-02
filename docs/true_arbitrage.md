# True And Hedged Arbitrage

Polylens separates executable arbitrage from probability-gap opportunities.

## Opportunity Types

- `true_arbitrage`: equivalent markets where all outcomes are covered and total hedge cost is below 1.00.
- `cross_market_hedge`: highly related markets with residual basis, settlement, or scope risk.
- `positive_ev`: probability-gap opportunities that are not guaranteed arbitrage.

## True YES/NO Arbitrage

For equivalent binary markets Polylens checks:

- Polymarket YES + Kalshi NO < 1.00
- Kalshi YES + Polymarket NO < 1.00

True arbitrage candidates include buy venues/outcomes, total cost, guaranteed profit, guaranteed ROI, stake ratio, and optional stake sizing when a bankroll is supplied.

## Sportsbook Hedges

Sportsbook candidates are `positive_ev` by default. They are only promoted to true arbitrage when a sportsbook side and a prediction-market NO/field side fully cover all outcomes. Related but imperfect structures are classified as `cross_market_hedge` and include `residual_risk`.

## CLI

```bash
python -m src.cli scan-true-arb --keyword knicks --sport basketball_nba --bankroll 1000 --json
python -m src.cli scan-true-arb --keyword knicks --sport basketball_nba --include-hedges
```


## Hedge-Leg Discovery

`find-hedges` explains whether a complementary NO/field leg is available:

```bash
python -m src.cli find-hedges --keyword knicks --sport basketball_nba --json
```

Diagnostics include `missing hedge leg`, `partial hedge`, `full hedge`, and `settlement mismatch`. A discovered field or NO leg can upgrade a sportsbook/prediction-market structure into true arbitrage only when coverage is complete and total outcome cost is below payout.
