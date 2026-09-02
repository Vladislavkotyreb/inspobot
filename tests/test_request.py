"""Форма запроса к Messages API и расчёт времени следующего запуска."""

import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from inspobot.bot import next_run_at
from inspobot.config import Config
from inspobot.curator import FALLBACK_BETA, MCP_BETA, build_request, drop_fallbacks

MSK = ZoneInfo("Europe/Moscow")


def config(**over):
    for key in list(os.environ):
        if key.startswith(("INSPOBOT_", "MOBBIN_", "TELEGRAM_", "ANTHROPIC_")):
            del os.environ[key]
    base = Config.from_env()
    return Config(**{**base.__dict__, **over})


class BuildRequestTest(unittest.TestCase):
    def test_mcp_server_and_toolset_are_paired(self):
        request = build_request(config(), "tok")
        self.assertEqual(request["mcp_servers"][0]["authorization_token"], "tok")
        self.assertEqual(
            request["tools"][0]["mcp_server_name"], request["mcp_servers"][0]["name"]
        )
        self.assertIn(MCP_BETA, request["betas"])

    def test_structured_output_is_requested(self):
        request = build_request(config(), "tok")
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")
        self.assertIn("picks", request["output_config"]["format"]["schema"]["properties"])

    def test_fallbacks_can_be_switched_off(self):
        self.assertNotIn("fallbacks", build_request(config(server_fallbacks=False), "t"))
        self.assertEqual(build_request(config(server_fallbacks=True), "t")["fallbacks"], "default")

    def test_fallbacks_are_dropped_on_a_fallback_error(self):
        request = build_request(config(server_fallbacks=True), "t")
        self.assertFalse(drop_fallbacks(request, "overloaded_error"))
        self.assertTrue(drop_fallbacks(request, "betas: unsupported fallback beta"))
        self.assertNotIn("fallbacks", request)
        self.assertNotIn(FALLBACK_BETA, request["betas"])
        self.assertIn(MCP_BETA, request["betas"])
        self.assertFalse(drop_fallbacks(request, "fallback"))  # второй раз нечего снимать


class NextRunTest(unittest.TestCase):
    def test_today_if_still_ahead(self):
        now = datetime(2026, 9, 2, 9, 30, tzinfo=MSK)
        self.assertEqual(next_run_at(now, 11, 0), datetime(2026, 9, 2, 11, 0, tzinfo=MSK))

    def test_tomorrow_if_already_passed(self):
        now = datetime(2026, 9, 2, 11, 0, 1, tzinfo=MSK)
        self.assertEqual(next_run_at(now, 11, 0), datetime(2026, 9, 3, 11, 0, tzinfo=MSK))

    def test_exactly_on_time_moves_to_next_day(self):
        now = datetime(2026, 9, 2, 11, 0, tzinfo=MSK)
        self.assertEqual(next_run_at(now, 11, 0), datetime(2026, 9, 3, 11, 0, tzinfo=MSK))


if __name__ == "__main__":
    unittest.main()
