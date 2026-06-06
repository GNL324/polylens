# Kalshi Edge Pipeline

This pipeline makes Kalshi edge and regime analysis reproducible from local files.

## 1. Export Account History

```bash
python -m src.cli kalshi-export-account-history --json
```

Default output:

```text
data/raw/kalshi_account_history.json
```

The export uses authenticated read-only Kalshi calls for balance, positions, orders, and fills. If credentials are unavailable, the command returns an `auth_error` result. It does not print API keys, private key contents, or other secrets.

Use a custom output path:

```bash
python -m src.cli kalshi-export-account-history --output data/raw/my_kalshi_history.json --json
```

## 2. Run Edge Analysis

```bash
python -m src.cli kalshi-edge-analysis \
  --account-history-path data/raw/kalshi_account_history.json \
  --snapshot-path data/kalshi_market_data.db \
  --export \
  --json
```

Outputs:

```text
data/reports/kalshi_edge_analysis.json
data/reports/kalshi_edge_analysis.csv
```

The report includes the edge classification, ranked variants, sample size, confidence level, and data quality warnings. Missing account history, missing snapshots, or too few usable trades are reported explicitly.

## 3. Run Market Regime Analysis

```bash
python -m src.cli market-regime-analysis --json
```

Inputs read by default:

```text
data/kalshi_market_data.db
data/raw/kalshi_account_history.json
data/reports/kalshi_edge_analysis.json
```

Outputs:

```text
data/reports/market_regime_analysis.json
data/reports/market_regime_analysis.csv
```

## Safety

This workflow is analytics-only. It does not enable live trading. Kalshi `place_order` and `cancel_order` remain `write_blocked`.
