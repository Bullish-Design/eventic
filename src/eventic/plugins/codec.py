"""Codec seam: how a version's state is represented in a row.

Default: ``FullSnapshot`` (the validated model dump per version). Optional:
``DiffStorage`` (forward deltas + snapshot-every-K). Exclusive seam.
"""
