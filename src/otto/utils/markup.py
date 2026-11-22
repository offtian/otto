"""
Channel markup: an agent writes Markdown, and each channel reads something
else.

  slack_mrkdwn   Slack's mrkdwn: *bold*, _italic_, `code`, <url|text>
  jira_wiki      Jira wiki markup: *bold*, _italic_, {{code}}, [text|url]

Only the marks a reply actually uses are converted — bold, italic, inline
code, code blocks and links — plus a heading, which becomes a bold line.
Everything else stays as written: a list bullet, ``2 * 3`` and
``my_file_name`` are not marks.

For Slack the words are also escaped (``&``, ``<``, ``>``). A reply is written
from untrusted content; unescaped, it could mention a whole channel or pass a
link off as something it is not.

After waku-agent's ``senders/format.py``, which makes the same split: the
model writes one dialect and the sender translates.
"""

import re
from collections.abc import Callable

import attrs


_FENCE = re.compile(r"```(?:[^\n`]*\n)?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
# One mark on each side, touching the words inside and no word or mark
# outside — so a bullet, a product and a name with underscores are left alone.
_ITALIC = re.compile(
    r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])"
    r"|(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])"
)
# A converted piece is parked under one of these until the end, so no later
# rule reads a mark inside code, inside a link, or inside finished markup.
_PARKED = re.compile("\x00(\\d+)\x00")


@attrs.frozen
class _Dialect:
    """
    How one channel writes each mark.
    """

    escape: Callable[[str], str]
    bold: Callable[[str], str]
    italic: Callable[[str], str]
    code: Callable[[str], str]
    block: Callable[[str], str]
    # (words, url)
    link: Callable[[str, str], str]


def slack_mrkdwn(markdown: str) -> str:
    """
    Return Markdown as Slack mrkdwn, with everything outside the converted
    marks escaped.
    """
    return _render(markdown, _SLACK)


def jira_wiki(markdown: str) -> str:
    """
    Return Markdown as Jira wiki markup.
    """
    return _render(markdown, _JIRA)


def _render(markdown: str, dialect: _Dialect) -> str:
    parked: list[str] = []

    def park(piece: str) -> str:
        parked.append(piece)
        return f"\x00{len(parked) - 1}\x00"

    text = markdown.replace("\x00", "")
    text = _FENCE.sub(lambda found: park(dialect.block(dialect.escape(found[1]))), text)
    text = _INLINE_CODE.sub(lambda found: park(dialect.code(dialect.escape(found[1]))), text)
    text = _LINK.sub(lambda found: park(dialect.link(found[1], found[2])), text)
    text = dialect.escape(text)
    # A heading becomes one bold line; bold inside it would nest.
    text = _HEADING.sub(
        lambda found: "**" + found[1].replace("**", "").replace("__", "") + "**", text
    )
    # Italic before bold: a channel's bold may be a single asterisk, which
    # the italic rule would then read as its own.
    text = _ITALIC.sub(lambda found: park(dialect.italic(found[1] or found[2])), text)
    text = _BOLD.sub(lambda found: dialect.bold(found[1] or found[2]), text)
    while _PARKED.search(text):
        text = _PARKED.sub(lambda found: parked[int(found[1])], text)
    return text


def _escape_slack(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_SLACK = _Dialect(
    escape=_escape_slack,
    bold=lambda words: f"*{words}*",
    italic=lambda words: f"_{words}_",
    code=lambda words: f"`{words}`",
    block=lambda words: f"```{words}```",
    link=lambda words, url: f"<{_escape_slack(url)}|{_escape_slack(words)}>",
)

_JIRA = _Dialect(
    escape=lambda text: text,
    bold=lambda words: f"*{words}*",
    italic=lambda words: f"_{words}_",
    code=lambda words: "{{" + words + "}}",
    block=lambda words: "{code}\n" + words.rstrip("\n") + "\n{code}",
    link=lambda words, url: f"[{words}|{url}]",
)
