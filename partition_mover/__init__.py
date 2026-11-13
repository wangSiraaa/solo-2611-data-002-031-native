"""One-time single-month partition migration tool.

Moves rows that fell into the DEFAULT partition of a UTC range-partitioned
table into a newly created monthly partition, atomically, while bounded
concurrent writes are in flight.  See SOLUTION.md for the design rationale
and the exact transaction boundaries.
"""

__version__ = "1.0.0"
