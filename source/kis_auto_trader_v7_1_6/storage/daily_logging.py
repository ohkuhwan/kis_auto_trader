"""KST 날짜별 파일 로깅 핸들러.

프로세스가 자정을 넘어 계속 실행되더라도 각 로그 이벤트의 KST 날짜에
해당하는 <prefix>_YYYYMMDD.log 파일로 자동 전환합니다.
파일 크기 회전 대신 날짜별 파일을 사용하여 일자별 보관 원칙을 유지합니다.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


class KSTDailyFileHandler(logging.Handler):
    """각 로그 이벤트의 한국 날짜에 맞는 UTF-8 파일에 기록합니다."""

    def __init__(self, directory: str | Path, prefix: str, level: int = logging.NOTSET):
        super().__init__(level)
        self.directory = Path(directory)
        self.prefix = prefix
        self._current_date: str | None = None
        self._stream = None
        self.directory.mkdir(parents=True, exist_ok=True)

    def _ensure_stream(self) -> None:
        date_key = datetime.now(KST).strftime("%Y%m%d")
        if date_key == self._current_date and self._stream is not None:
            return
        if self._stream is not None:
            self._stream.flush()
            self._stream.close()
        path = self.directory / f"{self.prefix}_{date_key}.log"
        self._stream = path.open("a", encoding="utf-8")
        self._current_date = date_key

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_stream()
            message = self.format(record)
            self._stream.write(message + "\n")
            self.flush()
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        if self._stream is not None:
            self._stream.flush()

    def close(self) -> None:
        self.acquire()
        try:
            if self._stream is not None:
                self._stream.flush()
                self._stream.close()
                self._stream = None
            super().close()
        finally:
            self.release()


class KSTFormatter(logging.Formatter):
    """로그 타임스탬프도 파일명과 동일한 KST 기준으로 출력합니다."""

    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, KST).timetuple()
