# Getting Started

## Prerequisites

- Python 3.12 or newer
- A shell environment with access to the project root
- Optional: `ODDS_API_KEY` for sportsbook and player prop commands
- Optional: Telegram bot credentials for alerts

## Setup

```bash
cd /home/noel/polylens
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
```

## First Commands

Analyze a Polymarket wallet:

```bash
python -m src.cli analyze-wallet 0x0000000000000000000000000000000000000000
```

Scan player prop arbitrage:

```bash
export ODDS_API_KEY=...
python -m src.cli scan-prop-arb --sport basketball_nba --markets player_points --bankroll 1000 --json
```

Run the continuous prop scanner:

```bash
python -m src.cli watch-prop-arb --sport basketball_nba --markets player_points --interval 30 --bankroll 1000 --min-roi 0.01
```

## Data Locations

- `data/raw/`: raw API responses for debugging
- `data/reports/`: generated wallet reports
- `data/polylens.db`: live arbitrage storage
- `data/opportunities.db`: prop arbitrage opportunity storage
- `logs/`: runtime logs
