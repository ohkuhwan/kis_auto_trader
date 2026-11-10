"""pytest 실행을 운영 데이터에서 격리합니다.

setup.sh는 KIS_TEST_RUNTIME_DIR을 지정해 임시 디렉터리를 제공합니다.
개발자가 pytest를 직접 실행하는 경우에도 별도 임시 디렉터리를 생성하고
테스트 종료 시 그 디렉터리만 삭제합니다. 운영 logs/state/휴장일 캐시는 건드리지 않습니다.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Optional runtime packages may be absent in lightweight CI. Stub only import-time
# surfaces; tests that need real crypto/network behavior must install requirements.
try:
    import Crypto  # noqa: F401
except ImportError:
    import types
    crypto = types.ModuleType("Crypto")
    cipher = types.ModuleType("Crypto.Cipher")
    padding = types.ModuleType("Crypto.Util.Padding")

    class _AES:
        MODE_CBC = 2

        @staticmethod
        def new(*args, **kwargs):
            raise RuntimeError("CI stub: AES operations are not available in offline tests")

    cipher.AES = _AES
    padding.unpad = lambda data, block_size: data
    sys.modules["Crypto"] = crypto
    sys.modules["Crypto.Cipher"] = cipher
    sys.modules["Crypto.Cipher.AES"] = cipher
    sys.modules["Crypto.Util"] = types.ModuleType("Crypto.Util")
    sys.modules["Crypto.Util.Padding"] = padding

_external_runtime_dir = os.getenv("KIS_TEST_RUNTIME_DIR", "").strip()
_owned_runtime_dir = not bool(_external_runtime_dir)
_runtime_dir = Path(_external_runtime_dir or tempfile.mkdtemp(prefix="kis-auto-trader-pytest-"))
_runtime_dir.mkdir(parents=True, exist_ok=True)
# 테스트가 생성한 임시 경로를 검증 코드에서도 동일하게 참조할 수 있게 공개합니다.
os.environ["KIS_TEST_RUNTIME_DIR"] = str(_runtime_dir.resolve())

os.environ["KIS_TEST_MODE"] = "1"
os.environ["KIS_DATA_DIR"] = str(_runtime_dir / "json")
os.environ["KIS_LOG_DIR"] = str(_runtime_dir / "logs")
os.environ["STRATEGY_SIGNAL_LOG"] = str(_runtime_dir / "logs" / "strategy_signals.jsonl")
os.environ["STATE_FILE"] = str(_runtime_dir / "state.json")
os.environ["UNCERTAIN_ORDERS_FILE"] = str(_runtime_dir / "uncertain_orders.json")
# 주문 취소 응답/최종 대사 기록도 운영 데이터이므로 반드시 격리합니다.
os.environ["CANCEL_ORDERS_FILE"] = str(_runtime_dir / "cancel_orders.json")
os.environ["HOLIDAY_CACHE_FILE"] = str(_runtime_dir / "holiday_config.json")
os.environ["KIS_TOKEN_CACHE_FILE"] = str(_runtime_dir / "kis_tokens.json")
os.environ["KIS_ENABLE_TRADING"] = "false"
os.environ["KIS_PAPER"] = "true"
# Never inherit real account credentials into automated tests.
os.environ["KIS_APP_KEY"] = "TEST_ONLY"
os.environ["KIS_APP_SECRET"] = "TEST_ONLY"
os.environ["KIS_HTS_ID"] = "TEST_ONLY"
os.environ["KIS_CANO"] = "00000000"
os.environ.setdefault("PYTHONPYCACHEPREFIX", str(_runtime_dir / "pycache"))
# pytest imports test modules after conftest, so redirect their bytecode cache too.
sys.pycache_prefix = str(_runtime_dir / "pycache")


def pytest_sessionfinish(session, exitstatus):
    """직접 pytest를 실행한 경우 테스트 전용 임시 파일만 정리합니다."""
    if _owned_runtime_dir:
        shutil.rmtree(_runtime_dir, ignore_errors=True)
