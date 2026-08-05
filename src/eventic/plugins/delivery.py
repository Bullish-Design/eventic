"""Delivery seam: how an emitted event reaches its handlers, per named mode.

Default: ``SyncDelivery`` (``mode="sync"``, in-process, strictly post-commit).
A durable backend (DBOS outbox) is the opt-in ``eventic[dbos]`` extra.
"""
