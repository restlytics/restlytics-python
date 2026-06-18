"""Interval-union (sweep-line) helper used to compute per-category "self time".

Why union and not a plain sum: child spans can overlap (parallel HTTP calls,
async queries, nested instrumentation). Summing their durations double-counts the
wall-clock time. The union of intervals gives the real wall-clock time actually
spent inside that category, which is what the dashboard breakdown and the
ingestion service's self-time rollups expect.

We work in plain integer nanoseconds. Python ints are arbitrary precision, so
there is no overflow concern.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

Interval = Tuple[int, int]


def union_length(intervals: Sequence[Interval]) -> int:
    """Total wall-clock length covered by the union of ``[start, end]`` intervals.

    ``intervals`` is a sequence of ``(start_ns, end_ns)`` pairs. Order does not
    matter; overlapping and adjacent (touching) intervals merge.
    """
    if not intervals:
        return 0

    # Copy + sort by start so a single forward sweep can merge overlaps.
    ordered: List[Interval] = sorted(intervals, key=lambda iv: iv[0])

    total = 0
    cur_start, cur_end = ordered[0]

    for start, end in ordered[1:]:
        if start > cur_end:
            # Disjoint: bank the current run and start a new one.
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        elif end > cur_end:
            # Overlapping: extend the current run.
            cur_end = end

    total += cur_end - cur_start
    return total
