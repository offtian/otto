"""
Composition root for Otto.

``settings.py`` owns configuration *values*; this module owns the *objects*
built from them. Wire concrete adapters (HTTP clients, stores, vendor SDKs)
into ``Configuration`` here and access the wired instance from the
application/interfaces layers via ``get_config()``.

Layer rules (enforced by import-linter):

- ``config`` may import ``domain``, ``utils`` and ``settings``
- ``domain`` and everything below it must never import ``config``
"""

import functools

import attrs

from otto.settings import Settings, settings


@attrs.frozen
class Configuration:
    """
    Process-wide wiring of settings and adapters.
    """

    settings: Settings

    # Add wired adapters as the project grows, e.g.:
    #
    #     http: httpx.AsyncClient
    #     store: stores.PostgresStore


@functools.cache
def get_config() -> Configuration:
    """
    Return the process-wide configuration, building it on first access.
    """
    return Configuration(settings=settings)
