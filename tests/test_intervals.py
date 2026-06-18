"""Interval-union self-time tests -- pure stdlib, no third-party deps.

Covers the sweep-line union including overlapping / parallel children, which is
the whole reason self-time uses a union and not a sum.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from restlytics.intervals import union_length  # noqa: E402


class IntervalsTest(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(0, union_length([]))

    def test_single_interval(self):
        self.assertEqual(10, union_length([(0, 10)]))

    def test_disjoint_intervals_sum(self):
        # [0,10] + [20,25] = 10 + 5
        self.assertEqual(15, union_length([(0, 10), (20, 25)]))

    def test_overlapping_intervals_are_unioned_not_summed(self):
        # [0,10] and [5,15] overlap -> union [0,15] = 15 (NOT 10+10=20).
        self.assertEqual(15, union_length([(0, 10), (5, 15)]))

    def test_parallel_children_fully_overlapping(self):
        # Two parallel DB queries over the exact same window must not double-count.
        self.assertEqual(10, union_length([(0, 10), (0, 10)]))

    def test_parallel_children_partial_overlap(self):
        # Three parallel-ish HTTP calls with staggered overlap.
        self.assertEqual(20, union_length([(0, 10), (5, 15), (12, 20)]))

    def test_fully_contained_interval(self):
        # [2,4] inside [0,10] -> just 10.
        self.assertEqual(10, union_length([(0, 10), (2, 4)]))

    def test_adjacent_touching_intervals_merge(self):
        # [0,10] and [10,20] touch at 10 -> continuous [0,20] = 20.
        self.assertEqual(20, union_length([(0, 10), (10, 20)]))

    def test_unsorted_input_is_handled(self):
        self.assertEqual(15, union_length([(20, 25), (0, 10)]))

    def test_multiple_overlaps_chained(self):
        # [0,5],[3,8],[7,12] all chain -> [0,12] = 12.
        self.assertEqual(12, union_length([(0, 5), (3, 8), (7, 12)]))

    def test_zero_length_intervals(self):
        # Cache markers are zero-length; they contribute nothing on their own.
        self.assertEqual(0, union_length([(5, 5), (10, 10)]))

    def test_large_nanosecond_values(self):
        # Real epoch-ns values are ~1.7e18; Python ints handle them natively.
        base = 1_700_000_000_000_000_000
        self.assertEqual(
            500,
            union_length([(base, base + 500), (base + 100, base + 400)]),
        )

    def test_app_self_time_pattern(self):
        # Models the tracer's app calc: root_dur - union(all_children).
        root_dur = 100
        # [0,30] & [20,50] merge -> [0,50] (=50); [60,70] disjoint (=10). union=60.
        children = [(0, 30), (20, 50), (60, 70)]
        union = union_length(children)
        self.assertEqual(60, union)
        self.assertEqual(40, max(0, root_dur - union))


if __name__ == "__main__":
    unittest.main()
