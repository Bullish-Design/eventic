"""The closed five-seam plugin framework (PLUGINS.md).

``Plugin`` base, the ``Seam`` enum, the capability-token contract
(``provides``/``requires``), and the class assembler that validates a
``Record`` subclass's plugin set *at class definition* — never at import or
first call. The framework is extracted at Step 6; until then the defaults
are wired directly in the core.
"""
