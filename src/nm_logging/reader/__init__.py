"""Reading persisted records back for UI and export (§22, §24).

Application UI code reads through this abstraction rather than the file layout, so
the persistence backend can evolve without changing the event contract. Nothing
in the rest of ``src/`` imports from here: the write-side contract must not depend
on the reader, and ``tests/test_layering.py`` asserts that.
"""
