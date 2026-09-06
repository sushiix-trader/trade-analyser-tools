"""Small deterministic statistical helpers shared by analysis modules."""

from __future__ import annotations

import math
from typing import Sequence


def linear_quantile(values: Sequence[float], percentile: float) -> float | None:
    """Return a linearly interpolated percentile of an ordered sequence.

    The interpolation matches the historical drawdown percentile contract: a
    percentile is placed at ``(n - 1) * p`` between the two surrounding
    observations.  Callers are expected to provide values in ascending order.
    """

    if not values:
        return None
    position = (len(values) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(values[lower])
    return float(values[lower] + (values[upper] - values[lower]) * (position - lower))
