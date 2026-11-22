"""
Procedural memory: which runbooks a request is about.

A runbook is a Markdown file the agent reads on demand through its runbook
tools (``agent``). What it is called — its file name, and the topic of its
first heading, the part before the dash in "# VPN — connect or fix a broken
connection" — is what ``match`` compares a request with, so the agent can be
told which runbook to open instead of listing them all first.

The match is keyword overlap, after waku-agent's skill loader: no model and
no embeddings, and a score that can be worked out by hand.

  - A request word matches a runbook word when they are equal, or when one
    begins with the other and the shorter has five letters or more:
    "passwords" finds "password", and "print" finds "printer".
  - A hyphenated word also counts joined: "Wi-Fi" is "wifi".
  - A word counts for less the more runbooks are called by it: "setup" names
    three runbooks and so points at none, while "printer setup" points at one.

It is a hint. A request worded with none of a runbook's own words gets no
match, and the agent lists the runbooks itself, as it always could.

ponytail: only the name and the topic are matched, so "my new laptop" does
not find device-setup — "new laptop first boot" comes after the dash. Word a
topic for what a requester says; match whole headings behind a stop list if
hints turn out too rare.
"""

import collections
import pathlib
import re


# One word, or several joined by hyphens.
_COMPOUND = re.compile(r"[^\W_]+(?:-[^\W_]+)*")
# What separates a heading's topic from its description.
_DASH = re.compile(r"\s+[—–-]+\s+")
# Words shorter than this are not compared at all.
_SHORTEST_WORD = 3
# Shorter words match only when equal; from here, a shared beginning counts.
_SHARED_BEGINNING = 5
# A runbook is named once the words it shares add up to one word of its own.
# Just under 1, so three thirds still count.
_ENOUGH = 0.999


def match(*, text: str, runbooks_dir: pathlib.Path, limit: int = 2) -> list[str]:
    """
    Return the names of up to ``limit`` runbooks the text shares words with,
    best match first — names ``read_runbook`` accepts. A runbook is named
    only when what it shares is its own: one word no other runbook is called
    by, or several that others share. No runbooks, or no match, is an empty
    list.

    :raises OSError: if a runbook cannot be read.
    :raises ValueError: if a runbook is not UTF-8 text.
    """
    called = {
        path.stem: _words(f"{path.stem.replace('-', ' ')} {_topic(path)}")
        for path in sorted(runbooks_dir.glob("*.md"))
    }
    asked = _words(text)
    shared_by = collections.Counter(word for words in called.values() for word in words)
    scores = {
        name: sum(
            1 / shared_by[word] for word in words if any(_same(word, other) for other in asked)
        )
        for name, words in called.items()
    }
    matched = [name for name, score in scores.items() if score >= _ENOUGH]
    return sorted(matched, key=lambda name: (-scores[name], name))[:limit]


def _topic(path: pathlib.Path) -> str:
    with path.open(encoding="utf-8") as runbook:
        heading = runbook.readline()
    return _DASH.split(heading.lstrip("# ").strip(), maxsplit=1)[0]


def _words(text: str) -> set[str]:
    words: set[str] = set()
    for compound in _COMPOUND.findall(text.lower()):
        words.add(compound.replace("-", ""))
        words.update(compound.split("-"))
    return {word for word in words if len(word) >= _SHORTEST_WORD}


def _same(word: str, other: str) -> bool:
    shorter, longer = sorted((word, other), key=len)
    return word == other or (len(shorter) >= _SHARED_BEGINNING and longer.startswith(shorter))
