"""
Long-term memory contracts: resolved conversations go in, and agents get them
back out — through the retrieval gate before a run, or the ``search_memory``
tool during one — as "we saw this before; the fix was X".

Two ports. ``MemoryStore`` keeps and searches what is remembered (embedded
Cognee, ``vendors/cognee``). ``CaseDistiller`` is consolidation: it decides
what of a resolved conversation is worth keeping, and what goes in is its
case note, not the transcript (an LLM, ``vendors/llm``). None wired for
either means "not here", and everything degrades gracefully.
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


class CaseDistiller(Protocol):
    """
    Consolidation: turns a resolved conversation into what is worth keeping.
    """

    async def distill(self, *, transcript: str) -> str | None:
        """
        Return a compact case note for a resolved conversation — what was
        wrong, why, and what fixed it, with nothing that identifies a person
        — or None when the conversation holds nothing reusable.

        Never raises: a failed distillation is also None, so nothing
        unvetted is ever remembered.
        """
        ...
