# Market Inventory Diagnostics

`market-inventory` summarizes the Polymarket markets a wallet traded and the Kalshi markets inspected for cross-platform overlap.

```bash
source /home/noel/.venv/bin/activate
python -m src.cli market-inventory <wallet>
python -m src.cli market-inventory <wallet> --include-closed
python -m src.cli market-inventory <wallet> --json
```

The inventory reports market categories, crypto assets, sports leagues, open/closed/unknown status counts, parsed crypto and sports market types, unparsed examples, and top reasons no matches were available.

`explain-matches` includes the same inventory summary fields so zero-candidate cases can be separated into likely missing Kalshi coverage, closed or expired wallet markets, parser gaps, or rejected structured candidates.

Closed Kalshi inventory is requested with `--include-closed` by trying closed and settled status values in addition to the default open market query. Open-market behavior is unchanged for existing commands.
