# Match Diagnostics

`explain-matches` explains how Polylens generated or rejected cross-platform candidates without changing wallet report JSON output.

```bash
source /home/noel/.venv/bin/activate
python -m src.cli explain-matches <wallet>
python -m src.cli explain-matches <wallet> --json
```

The command reports:

- Polymarket markets inspected
- Kalshi markets inspected
- sports structured matches
- crypto structured matches
- fallback text matches
- top rejected candidate reasons
- accepted matches, when present

Diagnostics currently include structured crypto acceptance/rejection details with parsed Polymarket/Kalshi fields. This is especially useful for short-window crypto markets where apparently similar contracts may differ by asset, direction, baseline, or close window.
