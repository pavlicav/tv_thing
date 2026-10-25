import unittest
from datetime import datetime, timezone
from unittest import mock

from roku_control import baseball, days, sports


class GamesDayTests(unittest.TestCase):
    def test_the_day_turns_at_3_am_eastern(self) -> None:
        night = datetime(2026, 10, 25, 0, 30, tzinfo=days.EASTERN)
        self.assertEqual(days.today(night), "2026-10-24")
        self.assertEqual(days.today(night.replace(hour=2, minute=59)), "2026-10-24")
        self.assertEqual(days.today(night.replace(hour=3, minute=0)), "2026-10-25")
        self.assertEqual(days.today(night.replace(hour=23, minute=59)), "2026-10-25")
        # Any zone: 06:30 UTC is 02:30 Eastern.
        self.assertEqual(days.today(datetime(2026, 10, 25, 6, 30, tzinfo=timezone.utc)), "2026-10-24")

    def test_the_night_the_clocks_go_back_turns_at_3_am_on_the_clock(self) -> None:
        # 2026-11-01: 01:30 comes twice (EDT, then EST), and 02:59 EST is still the night before.
        for utc_hour, utc_minute in ((5, 30), (6, 30), (7, 59)):
            moment = datetime(2026, 11, 1, utc_hour, utc_minute, tzinfo=timezone.utc)
            self.assertEqual(days.today(moment), "2026-10-31")
        self.assertEqual(days.today(datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)), "2026-11-01")

    def test_the_schedules_default_to_the_games_day(self) -> None:
        with mock.patch.object(days, "today", return_value="2026-10-24"):
            self.assertEqual(sports._day(None), "2026-10-24")
            self.assertEqual(baseball._iso_date(None), "2026-10-24")
        self.assertEqual(sports._day("2026-10-25"), "2026-10-25")
        self.assertEqual(baseball._iso_date("2026-10-25"), "2026-10-25")


if __name__ == "__main__":
    unittest.main()
