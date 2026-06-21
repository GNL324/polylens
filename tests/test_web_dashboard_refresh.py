from src.web.dashboard import should_refresh_page
from src.web.mission_control import resolve_mission_control_tab, set_mission_control_tab


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


def test_mission_control_refresh_preserves_active_tab() -> None:
    state = {"active_tab": "Wallets"}

    assert set_mission_control_tab(state, state["active_tab"]) == "Wallets"
    assert state["active_tab"] == "Wallets"


def test_mission_control_refresh_defaults_when_tab_missing() -> None:
    assert resolve_mission_control_tab(None) == "Overview"
    assert resolve_mission_control_tab("Removed Tab") == "Overview"
