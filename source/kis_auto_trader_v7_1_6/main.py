"""호환 실행 진입점. 실제 애플리케이션 구현은 application.py에 있습니다.

`python main.py` 실행 방식은 유지합니다. import main으로 가져온 경우에는 기존
테스트·유지보수 코드의 monkeypatch 및 모듈 전역 접근까지 유지하도록 application
모듈 자체를 호환 모듈로 노출합니다. import만으로 로깅이나 KIS 연결은 시작하지 않습니다.
"""
from __future__ import annotations

import sys
import application as _application

if __name__ == "__main__":
    _application.main()
else:
    sys.modules[__name__] = _application
