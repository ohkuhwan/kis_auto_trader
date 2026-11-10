"""Telegram 알림 모듈. 인증정보/URL/메시지 본문은 로그에 남기지 않습니다."""
import logging
import os
import requests

log = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self):
        self.token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID', '').strip()
        self.enabled = bool(self.token and self.chat_id)

    def send(self, message: str) -> bool:
        if not self.enabled:
            return False
        try:
            response = requests.post(
                f'https://api.telegram.org/bot{self.token}/sendMessage',
                json={'chat_id': self.chat_id, 'text': str(message)[:4000]}, timeout=8)
            response.raise_for_status()
            payload = response.json()
            if not payload.get('ok'):
                # API 응답 원문에는 민감한 값이 포함될 가능성이 있으므로 기록하지 않습니다.
                raise RuntimeError('Telegram API rejected the message')
            return True
        except Exception as exc:
            # requests 예외 문자열에는 bot token이 포함된 URL이 들어갈 수 있습니다.
            log.warning('Telegram 전송 실패: error_type=%s', type(exc).__name__)
            return False
