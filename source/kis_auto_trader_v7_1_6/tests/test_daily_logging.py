import logging
from datetime import datetime as RealDateTime
from pathlib import Path
from zoneinfo import ZoneInfo

import storage.daily_logging as daily_logging
from storage.daily_logging import KSTDailyFileHandler


def test_handler_switches_file_when_kst_date_changes(tmp_path, monkeypatch):
    current = {"value": RealDateTime(2026, 10, 16, 23, 59, tzinfo=ZoneInfo("Asia/Seoul"))}

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            return current["value"].astimezone(tz) if tz else current["value"].replace(tzinfo=None)

    monkeypatch.setattr(daily_logging, "datetime", FakeDateTime)
    handler = KSTDailyFileHandler(tmp_path, "fills")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(f"test.daily.{id(handler)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.info("start")
        current["value"] = RealDateTime(2026, 10, 17, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        logger.info("after-midnight")
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert (Path(tmp_path) / "fills_20261016.log").read_text(encoding="utf-8") == "start\n"
    assert (Path(tmp_path) / "fills_20261017.log").read_text(encoding="utf-8") == "after-midnight\n"
