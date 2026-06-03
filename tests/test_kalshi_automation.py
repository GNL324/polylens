from unittest.mock import patch

from src.trading.executor import KalshiExecutor
from src.trading.kalshi_strategy import scan_markets_for_signals
from src.trading.paper import PaperPortfolio
from src.trading.risk import RiskConfig, RiskState, evaluate_order, normalize_order_signal


def test_risk_rejects_max_trade_dollars():
    decision = evaluate_order({"ticker": "KX", "side": "yes", "price": 80, "count": 100}, RiskConfig(max_trade_dollars=10))
    assert decision.accepted is False
    assert decision.reason == "max trade dollars exceeded"


def test_risk_rejects_max_open_exposure():
    state = RiskState(open_exposure=9.75)
    decision = evaluate_order({"ticker": "KX", "side": "yes", "price": 50, "count": 1}, RiskConfig(max_open_exposure=10), state=state)
    assert decision.accepted is False
    assert decision.reason == "max open exposure exceeded"


def test_risk_rejects_max_daily_loss():
    state = RiskState(daily_loss=50)
    decision = evaluate_order({"ticker": "KX", "side": "yes", "price": 50, "count": 1}, RiskConfig(max_daily_loss=50), state=state)
    assert decision.accepted is False
    assert decision.reason == "max daily loss exceeded"


def test_duplicate_signal_cooldown():
    state = RiskState(signal_timestamps={"KX|yes|0.5|1": 100})
    decision = evaluate_order({"ticker": "KX", "side": "yes", "price": 50, "count": 1}, RiskConfig(duplicate_signal_cooldown_seconds=300), state=state, now=120)
    assert decision.accepted is False
    assert decision.reason == "duplicate signal cooldown"


def test_normalize_order_signal_accepts_cents_and_dollars():
    assert normalize_order_signal({"ticker": "KX", "side": "YES", "price": 45, "count": 2}) == {"ticker": "KX", "side": "yes", "price": 0.45, "count": 2}
    assert normalize_order_signal({"ticker": "KX", "side": "no", "price": 0.45, "count": 2})["price"] == 0.45


def test_invalid_side_price_and_count_are_rejected():
    assert evaluate_order({"ticker": "KX", "side": "maybe", "price": 45, "count": 1}, RiskConfig()).reason == "side must be yes or no"
    assert evaluate_order({"ticker": "KX", "side": "yes", "price": -1, "count": 1}, RiskConfig()).reason == "price must be positive"
    assert evaluate_order({"ticker": "KX", "side": "yes", "price": 45, "count": 0}, RiskConfig()).reason == "count must be a positive integer"


def test_paper_order_default_no_live(tmp_path):
    journal = tmp_path / "paper.jsonl"
    executor = KalshiExecutor(RiskConfig(live_trading=False, dry_run=True, max_trade_dollars=50), portfolio=PaperPortfolio(journal_path=journal))
    result = executor.submit_order("KXTEST", "yes", 45, 2)
    assert result["accepted"] is True
    assert result["mode"] == "paper"
    assert result["order"]["price"] == 0.45
    assert result["order"]["status"] == "filled_paper"
    assert "KXTEST" in journal.read_text(encoding="utf-8")


def test_live_order_is_gated_and_not_placed():
    executor = KalshiExecutor(RiskConfig(live_trading=True, dry_run=False, max_trade_dollars=50))
    result = executor.submit_order("KXTEST", "yes", 45, 2)
    assert result["accepted"] is False
    assert result["mode"] == "live_disabled"


def test_strategy_generates_signal_from_mocked_kalshi_market():
    markets = [{"ticker": "KXTEST", "pricing": {"yes_ask": 35}}]
    assert scan_markets_for_signals(markets, max_price=0.4) == [{"ticker": "KXTEST", "side": "yes", "price": 0.35, "count": 1, "reason": "yes ask below strategy threshold"}]


@patch("src.adapters.kalshi.urlopen")
def test_kalshi_orderbook_with_mocked_response(mock_urlopen, tmp_path):
    from src.adapters.kalshi import KalshiClient

    class Response:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): return False
        def read(self): return b'{"orderbook":{"yes":[[45,10]],"no":[[55,8]]}}'

    mock_urlopen.return_value = Response()
    orderbook = KalshiClient(raw_dir=tmp_path).get_orderbook("KXTEST")
    assert orderbook["orderbook"]["yes"] == [[45, 10]]


@patch("src.cli.KalshiClient")
def test_kalshi_paper_scan_cli_smoke(mock_client, capsys):
    from src.cli import kalshi_paper_scan
    mock_client.return_value.get_markets.return_value = [{"ticker": "KXTEST", "pricing": {"yes_ask": 35}}]
    signals = kalshi_paper_scan(limit=5, max_price=0.4)
    assert signals[0]["ticker"] == "KXTEST"
    assert "KXTEST" in capsys.readouterr().out


def test_kalshi_paper_trade_cli_rejects_invalid_input(capsys):
    from src.cli import kalshi_paper_trade
    result = kalshi_paper_trade("KXTEST", "yes", 101, 1)
    assert result["accepted"] is False
    assert "price" in result["reason"]
    assert "price" in capsys.readouterr().out


def test_kalshi_status_defaults_to_paper(monkeypatch):
    from src.cli import kalshi_status
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    status = kalshi_status(as_json=True)
    assert status["live_trading"] is False
    assert status["dry_run"] is True
    assert status["paper_mode"] is True


@patch("src.trading.executor.urlopen")
def test_missing_telegram_env_vars_skip_notification(mock_urlopen, monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    executor = KalshiExecutor(RiskConfig(max_trade_dollars=50), portfolio=PaperPortfolio(journal_path=tmp_path / "paper.jsonl"))
    result = executor.submit_order("KXTEST", "yes", 45, 1)
    assert result["accepted"] is True
    mock_urlopen.assert_not_called()
