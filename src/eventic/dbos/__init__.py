"""Opt-in DBOS adapter (``pip install eventic[dbos]`` only).

``DurableEvents`` — the delivery-seam plugin whose outbox enqueues *ids*, not
pickled records (R-S1); ``durable`` (explicit DBOS step registration), ``queue``
(explicit queue handles), ``create_app`` (FastAPI + DBOS wiring). Importing
this package must never happen from the core import graph (I6).
"""
