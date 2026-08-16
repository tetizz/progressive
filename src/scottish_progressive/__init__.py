"""Scottish Progressive Chess research engine."""

from .model import ENGINE_VERSION, ProgressiveState, SeriesResult
from .rules import generate_series, play_series

__all__ = [
    "ENGINE_VERSION",
    "ProgressiveState",
    "SeriesResult",
    "generate_series",
    "play_series",
]
