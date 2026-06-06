from src.web.redaction import REDACTED, redact_mapping, redact_value


def test_secret_redaction_by_key_name() -> None:
    assert redact_value("KALSHI_API_KEY", "abc123") == REDACTED
    assert redact_value("private_key_path", "/tmp/key.pem") == REDACTED
    assert redact_value("normal_setting", "visible") == "visible"


def test_secret_redaction_nested_mapping() -> None:
    redacted = redact_mapping(
        {
            "POLYLENS_TOKEN": "token-value",
            "nested": {"password": "pw", "safe": "ok"},
            "items": [{"api_secret": "hidden", "label": "shown"}],
        }
    )
    assert redacted["POLYLENS_TOKEN"] == REDACTED
    assert redacted["nested"]["password"] == REDACTED
    assert redacted["nested"]["safe"] == "ok"
    assert redacted["items"][0]["api_secret"] == REDACTED
    assert redacted["items"][0]["label"] == "shown"

