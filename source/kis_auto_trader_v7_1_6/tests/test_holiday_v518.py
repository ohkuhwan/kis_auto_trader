from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from storage.holiday import HolidayChecker


class FakeApi:
    def __init__(self, paper=True, result=True):
        self.s = SimpleNamespace(paper=paper)
        self.calls = 0
        self.result = result

    def check_holiday(self, day):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_paper_weekday_uses_local_fallback_without_api(tmp_path, monkeypatch):
    monkeypatch.setenv('KIS_PAPER_HOLIDAY_FALLBACK', 'WEEKDAY')
    monkeypatch.setenv('KIS_PAPER_HOLIDAYS', '')
    api = FakeApi(paper=True)
    checker = HolidayChecker(api, tmp_path / 'holiday.json')
    assert checker.is_business_day('20261014') is True  # Wednesday
    assert api.calls == 0


def test_paper_weekend_is_not_business_day(tmp_path, monkeypatch):
    monkeypatch.setenv('KIS_PAPER_HOLIDAY_FALLBACK', 'WEEKDAY')
    monkeypatch.setenv('KIS_PAPER_HOLIDAYS', '')
    checker = HolidayChecker(FakeApi(paper=True), tmp_path / 'holiday.json')
    assert checker.is_business_day('20261017') is False  # Saturday


def test_paper_manual_holiday_is_not_business_day(tmp_path, monkeypatch):
    monkeypatch.setenv('KIS_PAPER_HOLIDAY_FALLBACK', 'WEEKDAY')
    monkeypatch.setenv('KIS_PAPER_HOLIDAYS', '20261014,20261225')
    checker = HolidayChecker(FakeApi(paper=True), tmp_path / 'holiday.json')
    assert checker.is_business_day('20261014') is False


def test_paper_strict_mode_stops_without_api_call(tmp_path, monkeypatch):
    monkeypatch.setenv('KIS_PAPER_HOLIDAY_FALLBACK', 'STRICT')
    api = FakeApi(paper=True)
    checker = HolidayChecker(api, tmp_path / 'holiday.json')
    try:
        checker.is_business_day('20261014')
    except RuntimeError as exc:
        assert 'CTCA0903R' in str(exc)
    else:
        raise AssertionError('STRICT mode must fail closed')
    assert api.calls == 0


def test_real_mode_calls_api_and_caches_only_real_source(tmp_path):
    api = FakeApi(paper=False, result=True)
    cache = tmp_path / 'holiday.json'
    checker = HolidayChecker(api, cache)
    assert checker.is_business_day('20261014') is True
    assert api.calls == 1
    # API source cache is reusable in real mode
    assert checker.is_business_day('20261014') is True
    assert api.calls == 1


def test_paper_result_never_uses_real_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('KIS_PAPER_HOLIDAY_FALLBACK', 'WEEKDAY')
    monkeypatch.setenv('KIS_PAPER_HOLIDAYS', '')
    cache = tmp_path / 'holiday.json'
    cache.write_text('{"20261014":{"business_day":false,"source":"kis_api","mode":"real"}}', encoding='utf-8')
    api = FakeApi(paper=True)
    checker = HolidayChecker(api, cache)
    assert checker.is_business_day('20261014') is True
    assert api.calls == 0


def test_paper_2026_calendar_holiday_is_not_business_day(tmp_path, monkeypatch):
    monkeypatch.setenv('KIS_PAPER_HOLIDAY_FALLBACK', 'WEEKDAY')
    monkeypatch.setenv('KIS_PAPER_HOLIDAYS', '')
    monkeypatch.setenv('KIS_PAPER_HOLIDAY_CALENDAR', str(ROOT / 'config' / 'krx_holidays_2026.json'))
    api = FakeApi(paper=True)
    checker = HolidayChecker(api, tmp_path / 'holiday.json')
    assert checker.is_business_day('20261225') is False
    assert checker.is_business_day('20260505') is False
    assert checker.is_business_day('20261014') is True
    assert api.calls == 0


def test_paper_calendar_additional_manual_holiday(tmp_path, monkeypatch):
    monkeypatch.setenv('KIS_PAPER_HOLIDAY_FALLBACK', 'WEEKDAY')
    monkeypatch.setenv('KIS_PAPER_HOLIDAYS', '2026-10-14')
    monkeypatch.setenv('KIS_PAPER_HOLIDAY_CALENDAR', str(ROOT / 'config' / 'krx_holidays_2026.json'))
    checker = HolidayChecker(FakeApi(paper=True), tmp_path / 'holiday.json')
    assert checker.is_business_day('20261014') is False
