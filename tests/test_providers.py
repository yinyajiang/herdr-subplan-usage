import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from subplan.model import UsageError, timestamp, window
from subplan.providers import cursor_cookie, parse_codex, parse_cursor, parse_kimi, parse_zhipu


class ProviderTests(unittest.TestCase):
    def test_zhipu_credit_and_token_windows_ignore_mcp(self):
        value = parse_zhipu({"code": 200, "success": True, "data": {"limits": [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "percentage": 30,
             "usage": 1000, "currentValue": 300, "remaining": 600,
             "nextResetTime": 1791543600000},
            {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 21,
             "nextResetTime": 1792062000000},
            {"type": "TIME_LIMIT", "unit": 3, "number": 5, "percentage": 90}
        ]}})
        self.assertEqual(value["windows"]["5h"]["remaining_percent"], 60)
        self.assertEqual(value["windows"]["7d"]["remaining_percent"], 79)
        self.assertEqual(value["windows"]["5h"]["resets_at"], 1791543600)

    def test_missing_is_unknown_not_full(self):
        for parser, data in ((parse_zhipu, {"data": {"limits": []}}),
                             (parse_kimi, {}), (parse_codex, {}), (parse_cursor, {})):
            parsed = parser(data)
            self.assertEqual(parsed["status"], "unavailable")
            self.assertTrue(all(w["remaining_percent"] is None for w in parsed["windows"].values()))

    def test_kimi_legacy_count_schema(self):
        parsed = parse_kimi({
            "usage": {"limit": "100", "remaining": "60", "resetTime": "2026-10-16T10:00:00Z"},
            "limits": [{"window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                        "detail": {"limit": 100, "used": 12, "resetTime": "2026-10-09T10:00:00Z"}}]
        })
        self.assertEqual(parsed["windows"]["7d"]["remaining_percent"], 60)
        self.assertEqual(parsed["windows"]["5h"]["remaining_percent"], 88)

    def test_kimi_ratio_pools_precedence_and_monthly_not_weekly(self):
        parsed = parse_kimi({"usage": {"limit": 100, "remaining": 99}, "usages": {
            "limit_5h": {"used_ratio": 0.36, "reset_time": "2026-10-09T10:00:00Z"},
            "limit_7d": {"used_ratio": 0.8}, "limit_month_total": {"used_ratio": 0.2}
        }, "limits": [{"window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
                       "detail": {"limit": 100, "remaining": 100}}]})
        self.assertEqual(parsed["windows"]["5h"]["remaining_percent"], 64)
        self.assertEqual(parsed["windows"]["7d"]["remaining_percent"], 20)
        self.assertIsNone(parse_kimi({"usages": {"limit_5h": {"used_ratio": 0.5}},
                                      "usage": {"limit": 100, "remaining": 99}})["windows"]["7d"]["remaining_percent"])

    def test_codex_map_duration_not_primary_secondary_position(self):
        parsed = parse_codex({"rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 22, "windowDurationMins": 10080, "resetsAt": 1792062000},
                      "secondary": {"usedPercent": 10, "windowDurationMins": 300}},
            "spark": {"primary": {"usedPercent": 99, "windowDurationMins": 300}}
        }})
        self.assertEqual(parsed["windows"]["5h"]["remaining_percent"], 90)
        self.assertEqual(parsed["windows"]["7d"]["remaining_percent"], 78)
        week_only = parse_codex({"rateLimits": {"primary": {"usedPercent": 22, "windowDurationMins": 10080}}})
        self.assertIsNone(week_only["windows"]["5h"]["remaining_percent"])

    def test_cursor_monthly_amounts_and_fractional_percentage(self):
        parsed = parse_cursor({"billingCycleEnd": "2026-11-01T00:00:00Z", "individualUsage": {
            "plan": {"used": 500, "limit": 2000, "totalPercentUsed": 90},
            "onDemand": {"used": 30000, "limit": 50000}}})
        self.assertEqual(set(parsed["windows"]), {"month"})
        monthly = parsed["windows"]["month"]
        self.assertEqual(monthly["remaining_percent"], 10)
        self.assertEqual(monthly["quota_source"], "percentage")
        self.assertIsNone(monthly["used_amount"])
        self.assertIsNone(monthly["limit_amount"])
        partial = parse_cursor({"individualUsage": {"plan": {"totalPercentUsed": 0.36}}})
        self.assertAlmostEqual(partial["windows"]["month"]["remaining_percent"], 99.64)

    def test_cursor_included_percentage_overrides_exhausted_base_dollar_counter(self):
        parsed = parse_cursor({"billingCycleEnd": "2026-10-23T07:52:32Z", "individualUsage": {"plan": {
            "used": 2000, "limit": 2000, "remaining": 0,
            "breakdown": {"included": 2000, "bonus": 14398, "total": 16398},
            "autoPercentUsed": 36.37111111111111, "apiPercentUsed": 2.7555555555555555,
            "totalPercentUsed": 35.55121951219512}}})
        monthly = parsed["windows"]["month"]
        self.assertAlmostEqual(monthly["remaining_percent"], 64.44878048780488)
        self.assertAlmostEqual(monthly["used_percent"], 35.55121951219512)
        self.assertAlmostEqual(monthly["api_remaining_percent"], 97.24444444444444)
        self.assertIsNone(monthly["used_amount"])

    def test_cursor_api_percentage_missing_zero_and_exhausted(self):
        for used, expected in ((None, None), (-1, None), (0, 100), (100, 0), (105, 0)):
            parsed = parse_cursor({"individualUsage": {"plan": {"apiPercentUsed": used}}})
            self.assertEqual(parsed["windows"]["month"]["api_remaining_percent"], expected)

    def test_cursor_amount_fallback_and_zero_percentage(self):
        missing = parse_cursor({"individualUsage": {"plan": {"used": 500, "limit": 2000}}})
        self.assertEqual(missing["windows"]["month"]["remaining_percent"], 75)
        self.assertEqual(missing["windows"]["month"]["used_amount"], 5)
        zero = parse_cursor({"individualUsage": {"plan": {"totalPercentUsed": 0, "used": 2000, "limit": 2000}}})
        self.assertEqual(zero["windows"]["month"]["remaining_percent"], 100)

    def test_cursor_does_not_use_shared_team_pool(self):
        parsed = parse_cursor({"teamUsage": {"pooled": {"used": 100, "limit": 10000}}})
        self.assertIsNone(parsed["windows"]["month"]["remaining_percent"])

    def test_bad_numbers_and_ambiguous_timestamps(self):
        for value in (None, "nan", "inf", True, "bad"):
            self.assertIsNone(window(value)["remaining_percent"])
        self.assertEqual(window(200)["remaining_percent"], 0)
        self.assertIsNone(window(-5)["remaining_percent"])
        self.assertIsNone(timestamp("2026-10-09T18:30:00"))
        self.assertEqual(timestamp(1791543600000), timestamp(1791543600))

    def test_cursor_sqlite_login_cookie_reads_live_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.vscdb"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            payload = base64.urlsafe_b64encode(json.dumps({"sub": "auth0|user_123", "exp": time.time() + 3600}).encode()).decode().rstrip("=")
            token = "header." + payload + ".signature"
            connection.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/accessToken", token))
            connection.commit()
            cookie = cursor_cookie(path)
            self.assertTrue(cookie.startswith("WorkosCursorSessionToken=user_123%3A%3A"))
            self.assertEqual(connection.execute("SELECT value FROM ItemTable").fetchone()[0], token)
            connection.close()

    def test_cursor_missing_login_and_expired_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.vscdb"
            with self.assertRaises(UsageError):
                cursor_cookie(path)
            with closing(sqlite3.connect(path)) as database:
                database.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
                payload = base64.urlsafe_b64encode(b'{"sub":"user_123","exp":1}').decode().rstrip("=")
                database.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/accessToken", "h." + payload + ".s"))
                database.commit()
            with self.assertRaisesRegex(UsageError, "过期"):
                cursor_cookie(path)


if __name__ == "__main__":
    unittest.main()
