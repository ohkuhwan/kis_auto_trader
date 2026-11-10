"""Configuration package with lazy public settings imports.

Path-only utilities (config.paths) must remain importable before third-party
runtime dependencies are installed, including during first-time setup.
"""
from __future__ import annotations

__all__ = ["Settings", "StrategyConfig", "load_settings"]

def __getattr__(name: str):
    if name in __all__:
        from . import settings
        return getattr(settings, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
