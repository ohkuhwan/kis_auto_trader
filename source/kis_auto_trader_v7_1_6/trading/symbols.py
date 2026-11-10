"""종목코드에 대응하는 이름을 공유해 운영 로그의 가독성을 높입니다."""
from __future__ import annotations
import threading

_lock = threading.RLock()
_names: dict[str, str] = {}

def register_symbol(symbol: str, name: str = "") -> None:
    code = str(symbol or "").strip().zfill(6)
    label = str(name or "").strip()
    if code and code != "000000" and label and label != code:
        with _lock:
            _names[code] = label

def symbol_name(symbol: str) -> str:
    code = str(symbol or "").strip().zfill(6)
    with _lock:
        return _names.get(code, "")

def symbol_label(symbol: str, name: str = "") -> str:
    code = str(symbol or "").strip().zfill(6)
    if name:
        register_symbol(code, name)
    with _lock:
        label = _names.get(code, "")
    return f"{code}({label or '종목명 미확인'})" if code and code != "000000" else code
