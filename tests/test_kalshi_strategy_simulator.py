from unittest.mock import patch

from src.analysis.kalshi_strategy_simulator import SimulationConfig, export_kalshi_simulation, parse_price_bands, simulate_kalshi_strategy
from src.adapters.kalshi import KalshiAuthConfig, KalshiAuthenticatedClient


def _history():
    fills = {
        "fills": [
            {"ticker": "KXBTC15M", "side": "yes", "price": 5, "count": 10, "fee": 1, "realized_pnl": 200, "title": "Bitcoin up"},
            {"ticker": "KXETH15M", "side": "yes", "price": 95, "count": 4, "fee": 1, "realized_pnl": -50, "title": "Ethereum up"},
            {"ticker": "KXSOL15M", "side": "yes", "price": 50, "count": 3, "fee": 1, "realized_pnl": 25, "title": "Solana up"},
        ]
    }
    return {"balance": 10000}, {"positions": []}, {"orders": []}, fills


def test_parse_price_bands():
    assert parse_price_bands("0.01-0.10,90-99") == [(0.01, 0.1), (0.9, 0.99)]


def test_extreme_probability_simulation_filters_and_scores():
    result = simulate_kalshi_strategy(*_history(), SimulationConfig(assets={"BTC", "ETH"}, market_types={"crypto"}, price_bands=[(0.01, 0.1), (0.9, 0.99)], max_contracts=5, bankroll=1000, fee_assumption=0.01, strategy_mode="extreme-probability"))
    assert result["summary"]["simulated_trades"] == 2
    assert result["summary"]["fees"] > 0
    assert result["summary"]["best_trade"]["ticker"] == "KXBTC15M"
    assert result["summary"]["worst_trade"]["ticker"] == "KXETH15M"
    assert result["summary"]["strategy_classification"] == "historically_profitable_simulation"


def test_no_trade_baseline_has_no_trades():
    result = simulate_kalshi_strategy(*_history(), SimulationConfig(strategy_mode="no-trade-baseline"))
    assert result["summary"]["simulated_trades"] == 0
    assert result["summary"]["strategy_classification"] == "no_trade_baseline"


def test_mean_reversion_selects_mid_prices():
    result = simulate_kalshi_strategy(*_history(), SimulationConfig(strategy_mode="mean-reversion"))
    assert result["summary"]["simulated_trades"] == 1
    assert result["trades"][0]["ticker"] == "KXSOL15M"


def test_export_kalshi_simulation(tmp_path):
    result = simulate_kalshi_strategy(*_history(), SimulationConfig(strategy_mode="extreme-probability"))
    files = export_kalshi_simulation(result, tmp_path)
    assert files["json"].endswith("kalshi_simulation.json")
    assert files["csv"].endswith("kalshi_simulation.csv")
    assert "simulated_pnl" in (tmp_path / "kalshi_simulation.csv").read_text(encoding="utf-8")


@patch("src.cli.KalshiAuthenticatedClient")
def test_kalshi_simulate_cli_smoke(mock_client):
    from src.cli import kalshi_simulate

    balance, positions, orders, fills = _history()
    client = mock_client.return_value
    client.get_balance.return_value = balance
    client.get_positions.return_value = positions
    client.get_orders.return_value = orders
    client.get_fills.return_value = fills
    result = kalshi_simulate(assets="BTC,ETH", market_types="crypto", price_bands="0.01-0.10,0.90-0.99", max_contracts=5, bankroll=1000, fee_assumption=0.01, strategy_mode="extreme-probability", export=True, as_json=True)
    assert result["summary"]["simulated_trades"] == 2
    assert result["exported_files"]["json"].endswith("kalshi_simulation.json")


def test_write_endpoints_still_blocked(tmp_path):
    key_path = tmp_path / "kalshi.key"
    key_path.write_text("fake", encoding="utf-8")
    client = KalshiAuthenticatedClient(KalshiAuthConfig(api_key_id="id", private_key_path=key_path, env="production"), raw_dir=tmp_path)
    assert client.place_order()["mode"] == "write_blocked"
    assert client.cancel_order("order")["mode"] == "write_blocked"
