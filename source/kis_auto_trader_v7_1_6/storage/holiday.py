"""KIS 휴장일 조회 및 모의투자용 안전한 로컬 대체 판정."""
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from config.paths import runtime_path, resolve_path

log = logging.getLogger(__name__)


class HolidayChecker:
    def __init__(self, api, cache_file=None):
        self.api = api
        self.path = runtime_path("holiday_cache") if cache_file is None else resolve_path(cache_file)

    def _paper_business_day(self, day: str) -> bool:
        """모의투자 API에서 미지원인 휴장일 TR 대신 요일+수동 휴장일을 사용합니다.

        모의투자에만 적용합니다. 실전 계좌는 KIS 휴장일 API 확인 실패 시 fail-closed 합니다.
        config/krx_holidays_YYYY.json의 연도별 KRX 휴장일을 먼저 적용하고,
        KIS_PAPER_HOLIDAYS에는 추가 휴장일을 YYYYMMDD 형식으로 지정할 수 있습니다.
        """
        fallback = os.getenv('KIS_PAPER_HOLIDAY_FALLBACK', 'WEEKDAY').strip().upper()
        if fallback not in {'WEEKDAY', 'STRICT'}:
            log.warning('알 수 없는 KIS_PAPER_HOLIDAY_FALLBACK=%s; STRICT로 처리합니다.', fallback)
            fallback = 'STRICT'
        if fallback == 'STRICT':
            raise RuntimeError(
                '모의투자 휴장일 API는 CTCA0903R 미지원일 수 있습니다. '
                'KIS_PAPER_HOLIDAY_FALLBACK=WEEKDAY로 설정하거나 휴장일 조회를 별도로 확인하십시오.'
            )
        try:
            parsed = datetime.strptime(day, '%Y%m%d')
        except ValueError as exc:
            raise ValueError(f'잘못된 날짜 형식: {day}; YYYYMMDD가 필요합니다.') from exc
        manual_holidays = {
            item.strip().replace('-', '') for item in os.getenv('KIS_PAPER_HOLIDAYS', '').split(',') if item.strip()
        }
        # 프로젝트 config 디렉터리의 연도별 KRX 달력 파일을 사용합니다.
        calendar_path_raw = os.getenv('KIS_PAPER_HOLIDAY_CALENDAR', '').strip()
        calendar_path = resolve_path(calendar_path_raw) if calendar_path_raw else resolve_path(f'config/krx_holidays_{parsed.year}.json')
        calendar_holidays = set()
        calendar_names = {}
        try:
            if calendar_path.exists():
                calendar_data = json.loads(calendar_path.read_text(encoding='utf-8'))
                for item in calendar_data.get('holidays', []):
                    date_value = str(item.get('date', '')).replace('-', '')
                    if date_value:
                        calendar_holidays.add(date_value)
                        calendar_names[date_value] = str(item.get('name', '휴장일'))
        except Exception:
            log.exception('모의투자 휴장일 달력 파일을 읽지 못했습니다: %s', calendar_path)
            raise RuntimeError(f'모의투자 휴장일 달력 파일을 읽지 못했습니다: {calendar_path}')

        if day in calendar_holidays:
            log.warning('모의투자 KRX 달력에 등록된 휴장일이므로 실행하지 않습니다: %s (%s)', day, calendar_names.get(day, '휴장일'))
            return False
        if day in manual_holidays:
            log.warning('모의투자 추가 휴장일 목록에 포함되어 실행하지 않습니다: %s', day)
            return False
        business = parsed.weekday() < 5
        if not calendar_path.exists():
            log.warning('해당 연도의 KRX 휴장일 달력 파일이 없습니다: %s; 평일/수동 목록으로만 판정합니다.', calendar_path)
        log.info(
            '모의투자 휴장일 API 미지원으로 로컬 달력 판정: date=%s business_day=%s calendar=%s. '
            '임시휴장일은 KIS_PAPER_HOLIDAYS에 추가하십시오.', day, business, calendar_path.name
        )
        return business

    def is_business_day(self, today=None) -> bool:
        day = today or datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d')
        # 모의투자에서 CTCA0903R은 EGW02006("모의투자 TR이 아닙니다")를 반환할 수 있으므로
        # 이 API를 호출하지 않고 평일/수동 휴장일 기준으로 테스트 실행 여부를 판단합니다.
        # 이 로컬 결과는 실전 모드에서 재사용하지 않습니다.
        if bool(getattr(getattr(self.api, 's', None), 'paper', False)):
            return self._paper_business_day(str(day))

        cache = {}
        try:
            if self.path.exists():
                cache = json.loads(self.path.read_text(encoding='utf-8'))
            entry = cache.get(day)
            # 실전 모드에서는 출처가 확인된 실전 API 결과만 캐시에서 재사용합니다. 출처 불명 캐시는 무효화합니다.
            if isinstance(entry, dict) and entry.get('source') == 'kis_api' and entry.get('mode') == 'real':
                return bool(entry['business_day'])
        except Exception:
            log.exception('휴장일 캐시 읽기 실패; API 재조회')

        result = self.api.check_holiday(day)
        business = bool(result)
        cache[day] = {
            'business_day': business,
            'checked_at': datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds'),
            'source': 'kis_api',
            'mode': 'real',
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(tmp, self.path)
        except Exception:
            log.exception('휴장일 캐시 저장 실패')
        return business
