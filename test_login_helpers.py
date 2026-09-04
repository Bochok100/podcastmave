import datetime
import unittest
from unittest.mock import patch

import bot


class DelayTests(unittest.TestCase):
    def test_valid_same_day(self):
        tz = datetime.timezone(datetime.timedelta(hours=-3))
        now = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=tz)
        with patch("bot.datetime") as dt:
            dt.datetime.now.return_value = now
            dt.datetime.side_effect = lambda *a, **k: datetime.datetime(*a, **k)
            dt.timedelta = datetime.timedelta
            dt.timezone = datetime.timezone
            # keep the real module for other attrs
            dt.datetime.now = lambda tz=None: now
            delay = bot.get_delay_seconds("14:30")
        self.assertEqual(delay, 4 * 3600 + 30 * 60)

    def test_invalid(self):
        self.assertEqual(bot.get_delay_seconds("99:99"), -1)
        self.assertEqual(bot.get_delay_seconds("nope"), -1)

    def test_dot_separator(self):
        tz = datetime.timezone(datetime.timedelta(hours=-3))
        now = datetime.datetime(2026, 9, 4, 10, 0, tzinfo=tz)
        with patch("bot.datetime") as dt:
            dt.timedelta = datetime.timedelta
            dt.timezone = datetime.timezone
            dt.datetime.now = lambda tz=None: now
            delay = bot.get_delay_seconds("10.15")
        self.assertEqual(delay, 15 * 60)


class DataDirTests(unittest.TestCase):
    def test_ensure_data_dir(self):
        bot.ensure_data_dir()
        self.assertTrue(bot.DATA_DIR.exists())
        self.assertTrue(bot.Path(bot.PROFILE_DIR).exists())


if __name__ == "__main__":
    unittest.main()
