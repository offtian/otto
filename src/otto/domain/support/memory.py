"""
Long-term memory contract: resolved conversations go in, and agents search
them back out through the ``search_memory`` tool — "we saw this before; the
fix was X". The embedded Cognee implementation lives in ``vendors/cognee``;
None wired means no memory, and everything degrades gracefully.
"""

from typing import Protocol


class MemoryStore(Protocol):
    """
    Persistence contract for Otto's long-term support memory.
    """

    async def ingest(self, *, text: str) -> None:
        """
        Store one resolved conversation and index it for retrieval.
        """
        ...

    async def search(self, *, query: str) -> list[str]:
        """
        Return memory passages relevant to the query, best first.
        """
        ...
