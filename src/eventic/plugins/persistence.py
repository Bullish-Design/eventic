"""Persistence seam: how & where version rows are stored and queried.

Default provider: ``SingleTableJSONB`` — the append-only ``records`` table
with the loud-conflict append (I1/I4/I5). Exclusive seam.
"""
