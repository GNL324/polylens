import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from src import cli
from src.intelligence.falcon_source import FalconClient, FalconWalletSource


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FalconSourceTests(unittest.TestCase):
    def test_missing_api_key_is_disabled_and_inert(self):
        with patch.dict(os.environ, {"FALCON_API_KEY": ""}, clear=False):
            client = FalconClient()
            response = client.retrieve(584, {}, limit=50, offset=0)

        self.assertFalse(client.enabled)
        self.assertTrue(response["disabled"])
        self.assertEqual(response["data"]["results"], [])
        self.assertEqual(client.status().reason, "missing FALCON_API_KEY")

    def test_request_payload_shape(self):
        captured = {}

        def opener(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["auth"] = request.headers["Authorization"]
            return FakeResponse({"data": {"results": []}, "pagination": {"has_more": False}})

        client = FalconClient(api_key="test-key", opener=opener)
        client.retrieve(584, {"sort_by": "h_score"}, limit=25, offset=50)

        self.assertEqual(captured["auth"], "Bearer test-key")
        self.assertEqual(
            captured["body"],
            {
                "agent_id": 584,
                "params": {"sort_by": "h_score"},
                "pagination": {"limit": 25, "offset": 50},
                "formatter_config": {"format_type": "raw"},
            },
        )

    def test_wallet_normalization_handles_wallet_variants(self):
        source = FalconWalletSource(FalconClient(api_key=""))

        rows = [
            {"wallet": "0xwallet", "leaderboard_rank": 1, "h_score": 91.5},
            {"address": "0xaddress", "rank": 2},
            {"proxy_wallet": "0xproxy", "total_pnl": "10.5"},
        ]
        normalized = [source.normalize_wallet(row) for row in rows]

        self.assertEqual([row["wallet_address"] for row in normalized], ["0xwallet", "0xaddress", "0xproxy"])
        self.assertTrue(all(row["wallet_source"] == "falcon" for row in normalized))
        self.assertTrue(all(row["read_only"] for row in normalized))

    def test_roi_and_win_rate_are_preserved(self):
        source = FalconWalletSource(FalconClient(api_key=""))
        row = source.normalize_wallet({"address": "0xabc", "roi": "0.414952", "win_rate": "1.0000"})
        metrics = json.loads(row["source_metrics_json"])

        self.assertEqual(metrics["roi"], "0.414952")
        self.assertEqual(metrics["win_rate"], "1.0000")

    def test_falcon_candidates_remain_pending(self):
        source = FalconWalletSource(FalconClient(api_key=""))
        row = source.normalize_wallet({"wallet": "0xabc", "h_score": 88, "rank": 3})

        self.assertEqual(row["validation_status"], "pending")
        self.assertEqual(row["paper_gate_status"], "pending")
        self.assertNotIn("promoted_at", row)

    def test_cli_returns_read_only_without_promotion_fields(self):
        sample = {
            "data": {
                "results": [
                    {
                        "wallet": "0xabc",
                        "leaderboard_rank": 1,
                        "h_score": 99.1,
                        "roi_pct_15d": 0.42,
                        "win_rate_pct_15d": 0.61,
                    }
                ]
            },
            "pagination": {"has_more": False},
        }

        def opener(request, timeout):
            return FakeResponse(sample)

        output = io.StringIO()
        with patch.dict(os.environ, {"FALCON_API_KEY": "test-key"}, clear=False):
            with patch("src.intelligence.falcon_source.urllib.request.urlopen", opener):
                with patch.object(sys, "argv", ["polylens", "falcon-wallet-source", "--limit", "1", "--json"]):
                    with redirect_stdout(output):
                        cli.main()

        report = json.loads(output.getvalue())
        self.assertTrue(report["read_only"])
        self.assertEqual(len(report["candidates"]), 1)
        self.assertTrue(report["candidates"][0]["read_only"])
        self.assertEqual(report["candidates"][0]["validation_status"], "pending")
        self.assertEqual(report["candidates"][0]["paper_gate_status"], "pending")
        self.assertNotIn("promoted_at", report["candidates"][0])
        self.assertIn('"read_only": true', output.getvalue())


if __name__ == "__main__":
    unittest.main()
