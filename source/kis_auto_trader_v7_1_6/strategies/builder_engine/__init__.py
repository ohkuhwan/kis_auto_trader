"""Strategy engine package.

Imports are lazy so offline strategy tests do not load trading/WebSocket dependencies.
"""
__all__ = ["YamlStrategyEngine", "score_signal"]

def __getattr__(name):
    if name == "YamlStrategyEngine":
        from .yaml_engine import YamlStrategyEngine
        return YamlStrategyEngine
    if name == "score_signal":
        from .signal_strength import score_signal
        return score_signal
    raise AttributeError(name)
