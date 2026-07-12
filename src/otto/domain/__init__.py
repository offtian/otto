"""
Pure business logic, agnostic of which use-case calls it.

Package by domain category (``domain/<category>/``) with ``queries.py`` for
reads and ``operations.py`` for writes. Never imports ``config`` or
``settings`` — values arrive as parameters (enforced by import-linter).
"""
