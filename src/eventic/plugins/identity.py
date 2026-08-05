"""Identity seam: deterministic ``version_id`` derivation (I4).

Default: ``Uuid5Deterministic`` — ``uuid5(NAMESPACE_URL, "eventic:{id}:{version}")``
for every version including v0. Exclusive seam.
"""
