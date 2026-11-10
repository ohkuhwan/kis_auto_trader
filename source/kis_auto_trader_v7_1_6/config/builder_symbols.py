"""Single-source loading and normalization for manually configured Builder symbols."""
from __future__ import annotations
import logging
from config.paths import runtime_path


def normalize_symbols(items) -> list[str]:
    """Normalize stock codes, skip comments/blank lines, and preserve first-seen order."""
    result: list[str] = []
    for item in items:
        value = str(item).strip()
        if not value or value.startswith("#"):
            continue
        symbol = value.zfill(6)
        if len(symbol) == 6 and symbol.isdigit() and symbol not in result:
            result.append(symbol)
    return result


def load_builder_symbols(*, logger: logging.Logger | None = None) -> list[str]:
    """Read only BUILDER_SYMBOLS_FILE (default project-root/config/entry_symbols.txt).

    BUILDER_ENTRY_SYMBOLS is intentionally ignored. Relative paths are resolved from
    project root through config.paths, so changing process cwd cannot change the list.
    """
    path = runtime_path("builder_symbols")
    if not path.exists():
        if logger:
            logger.info("[STRATEGY_BUILDER] 수동 종목 목록 파일이 없습니다: %s", path)
        return []
    try:
        items = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        if logger:
            logger.exception("[STRATEGY_BUILDER] 수동 종목 목록을 읽을 수 없습니다: %s", path)
        return []
    symbols = normalize_symbols(items)
    if logger:
        logger.info("[STRATEGY_BUILDER] 수동 종목 목록 로드: file=%s count=%d", path, len(symbols))
        invalid = [str(item).strip() for item in items if str(item).strip() and not str(item).strip().startswith("#") and (len(str(item).strip().zfill(6)) != 6 or not str(item).strip().zfill(6).isdigit())]
        if invalid:
            logger.warning("[STRATEGY_BUILDER] 잘못된 종목코드 %d개를 제외했습니다: %s", len(invalid), ", ".join(invalid[:10]))
    return symbols


def combine_candidate_symbols(hts_symbols, manual_symbols) -> list[str]:
    """Union dynamic HTS candidates and manual Builder symbols, preserving source order."""
    return normalize_symbols([*(hts_symbols or []), *(manual_symbols or [])])
