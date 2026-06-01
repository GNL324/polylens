# Price-Aware Arbitrage Scanning

Polylens can enrich conservative Polymarket/Kalshi text matches with indicative pricing:

```bash
source /home/noel/.venv/bin/activate
python -m src.cli scan-arb <wallet>
python -m src.cli export-wallet <wallet> --include-kalshi --include-pricing
```

The JSON report includes `price_aware_arbitrage_candidates`.

## Pricing Sources

- Polymarket YES price is inferred from the wallet's own trade history. If the wallet bought or sold a NO-like outcome, Polylens converts it to an implied YES price with `1 - price`.
- Kalshi YES/NO prices come from public market fields such as `yes_ask_dollars`, `no_ask_dollars`, `yes_bid_dollars`, `no_bid_dollars`, and `last_price_dollars`.

## Conservative Behavior

Polylens only computes a theoretical edge when it has both a Polymarket implied YES price and Kalshi YES/NO ask prices. If any required price is missing, the candidate is marked `insufficient pricing data` and no edge is reported.

The calculation is informational only. It does not account for live Polymarket order-book depth, fees, slippage, settlement differences, withdrawal costs, market-rule mismatches, or exchange access constraints.
