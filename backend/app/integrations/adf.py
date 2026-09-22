"""Atlassian Document Format: just enough of it, built rather than written.

Jira's REST v3 takes rich text as ADF - a JSON tree - for `description`,
`comment.body` and rich-text custom fields. v2 still accepts a wiki-markup
string and is not deprecated, and using v2 to dodge this file would be a
defensible shortcut. It is not taken, for one reason: v2 has no future, and a
description assembled by string concatenation is where an alarm message
containing a `{` or a `|` starts rendering as a broken table on somebody's
service desk. A tree has no escaping problem to get wrong.

ONLY the node types actually used are here. ADF has dozens; a builder that
covers all of them is a schema reimplementation nobody maintains.

THE ONE RULE THAT BITES: a `text` node with an empty string is invalid, and
Jira rejects the whole document with a 400 that names the node and not the
field. Every constructor here drops empties instead of emitting them, which is
why `paragraph("")` returns a paragraph with no content rather than a
paragraph containing nothing.
"""

from __future__ import annotations

import json
from typing import Any

#: Jira truncates or rejects very large documents, and a varbind dump from a
#: chatty device can be megabytes. Truncate here, visibly, rather than having
#: the create fail at 09:14 on a Sunday.
MAX_CODE_CHARS = 8000


def doc(*blocks: dict[str, Any] | None) -> dict[str, Any]:
    """A document. `None` blocks are dropped, so callers can inline a
    conditional without building a list first."""
    return {"type": "doc", "version": 1,
            "content": [b for b in blocks if b]}


def text(value: str, *marks: dict[str, Any]) -> dict[str, Any] | None:
    if not value:
        return None
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = list(marks)
    return node


def strong() -> dict[str, Any]:
    return {"type": "strong"}


def code() -> dict[str, Any]:
    return {"type": "code"}


def link(href: str) -> dict[str, Any]:
    return {"type": "link", "attrs": {"href": href}}


def paragraph(*parts: str | dict[str, Any] | None) -> dict[str, Any]:
    """A paragraph from a mix of plain strings and already-marked text nodes."""
    content = []
    for part in parts:
        if part is None:
            continue
        node = text(part) if isinstance(part, str) else part
        if node:
            content.append(node)
    return {"type": "paragraph", "content": content}


def heading(value: str, level: int = 3) -> dict[str, Any]:
    return {"type": "heading", "attrs": {"level": level},
            "content": [n for n in (text(value),) if n]}


def rule() -> dict[str, Any]:
    return {"type": "rule"}


def code_block(value: str, language: str = "json") -> dict[str, Any] | None:
    """A fenced block. Truncated visibly rather than silently."""
    if not value:
        return None
    if len(value) > MAX_CODE_CHARS:
        value = (value[:MAX_CODE_CHARS]
                 + f"\n... truncated, {len(value) - MAX_CODE_CHARS} more characters")
    return {"type": "codeBlock", "attrs": {"language": language},
            "content": [n for n in (text(value),) if n]}


def json_block(payload: Any) -> dict[str, Any] | None:
    """The raw record, pretty-printed.

    `default=str` because the payload carries datetimes, Decimals and UUIDs -
    the same three types the WebSocket fan-out had to learn about the hard
    way. json.dumps refuses all three, and a TypeError here would take down
    the dispatcher rather than the field.
    """
    return code_block(json.dumps(payload, indent=2, sort_keys=True, default=str))


def table(rows: list[tuple[str, str]], *, header: tuple[str, str] | None = None
          ) -> dict[str, Any] | None:
    """A two-column attribute table. Rows with an empty value are dropped.

    Dropped rather than rendered blank: a ticket that lists eight attributes
    and leaves five of them empty reads as a broken integration, and the
    reader cannot tell "we did not measure it" from "it is zero".
    """
    body = [(k, v) for k, v in rows if v not in (None, "")]
    if not body:
        return None
    content = []
    if header:
        content.append(_row(header, cell="tableHeader"))
    content.extend(_row(r) for r in body)
    return {"type": "table",
            "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
            "content": content}


def _row(pair: tuple[str, str], *, cell: str = "tableCell") -> dict[str, Any]:
    return {"type": "tableRow",
            "content": [{"type": cell, "attrs": {}, "content": [paragraph(str(v))]}
                        for v in pair]}


def to_text(document: dict[str, Any]) -> str:
    """Flatten a document to plain text.

    Needed because `POST /rest/servicedeskapi/request` has historically taken
    `description` as a plain STRING while `/rest/api/3/issue` demands ADF for
    the same field, and the sources disagree about whether that is still true.
    Rather than guess, the mapper builds one document and renders it either
    way; the connection test settles which the tenant wants.
    """
    out: list[str] = []
    _flatten(document.get("content") or [], out)
    return "\n".join(out).strip()


def _flatten(nodes: list[dict[str, Any]], out: list[str]) -> None:
    for node in nodes:
        kind = node.get("type")
        if kind == "text":
            # Reached only inside a block, whose own branch joins this buffer
            # with "" - so one text node per entry is what the caller wants.
            out.append(node.get("text", ""))
        elif kind == "rule":
            out.append("---")
        elif kind == "tableRow":
            # One line per row, columns tab-separated: the only rendering that
            # survives a plain-text field without pretending to be a grid.
            cells: list[str] = []
            for cell in node.get("content") or []:
                buf: list[str] = []
                _flatten(cell.get("content") or [], buf)
                cells.append(" ".join(b.strip() for b in buf))
            out.append("\t".join(cells))
        elif kind in ("paragraph", "heading", "codeBlock"):
            buf = []
            _flatten(node.get("content") or [], buf)
            out.append("".join(buf))
        else:
            _flatten(node.get("content") or [], out)
