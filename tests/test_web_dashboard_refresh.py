from src.web.dashboard import should_refresh_page


def test_live_opportunities_auto_refresh_pauses_for_detail_dialog() -> None:
    state = {"detail_dialog_open": True, "settlement_dialog_open": False}
    assert should_refresh_page("Live Opportunities", state) is False
    state["detail_dialog_open"] = False
    assert should_refresh_page("Live Opportunities", state) is True


def test_results_refresh_pauses_for_settlement_dialog() -> None:
    state = {"detail_dialog_open": False, "settlement_dialog_open": True}
    assert should_refresh_page("Results / P&L", state) is False
    state["settlement_dialog_open"] = False
    assert should_refresh_page("Results / P&L", state) is True


def test_other_pages_ignore_dialog_refresh_gate() -> None:
    state = {"detail_dialog_open": True, "settlement_dialog_open": True}
    assert should_refresh_page("Risk Engine", state) is True

