from __future__ import annotations

from .model import SeriesResult


def format_series_turn(series_number: int, series: SeriesResult) -> str:
    color = "White" if series_number % 2 else "Black"
    suffix = f"; {series.unused_moves} unused" if series.unused_moves else ""
    if series.outcome:
        suffix += f"; {series.outcome.value}"
    return f"S{series_number} {color}[{series_number}]: {series.notation}{suffix}"


def format_principal_variation(
    starting_series: int, variation: tuple[SeriesResult, ...]
) -> str:
    return " | ".join(
        format_series_turn(starting_series + offset, series)
        for offset, series in enumerate(variation)
    )
