"""The Atlassian Document Format builder.

The rule that bites, and the reason this file exists: a `text` node with an
empty string is INVALID ADF, and Jira rejects the whole document with a 400
naming the node rather than the field. Every constructor drops empties instead
of emitting them, and every assertion below is about that or about the
truncation that keeps a chatty device's varbind dump from failing a create.
"""

from __future__ import annotations

import json

from app.integrations import adf


def _texts(node):
    """Every text node in a document, flattened."""
    out = []
    if isinstance(node, dict):
        if node.get("type") == "text":
            out.append(node.get("text"))
        for child in node.get("content") or []:
            out.extend(_texts(child))
    return out


# ------------------------------------------------------------- empty nodes

def test_an_empty_string_never_becomes_a_text_node():
    assert adf.text("") is None
    assert adf.paragraph("") == {"type": "paragraph", "content": []}
    assert not _texts(adf.doc(adf.paragraph("")))


def test_none_blocks_are_dropped_from_a_document():
    """So a caller can inline a conditional instead of building a list."""
    doc = adf.doc(adf.paragraph("hello"), None, adf.code_block(""))
    assert len(doc["content"]) == 1


def test_a_document_declares_version_one():
    doc = adf.doc(adf.paragraph("x"))
    assert doc["type"] == "doc" and doc["version"] == 1


# ------------------------------------------------------------------ marks

def test_a_link_rides_on_the_text_node():
    node = adf.text("Open in the DCIM", adf.link("https://dcim.example.com/a"))
    assert node["marks"] == [{"type": "link",
                              "attrs": {"href": "https://dcim.example.com/a"}}]


def test_marks_are_omitted_when_there_are_none():
    assert "marks" not in adf.text("plain")


# ------------------------------------------------------------------ table

def test_a_row_with_an_empty_value_is_dropped():
    """A ticket listing eight attributes with five blank reads as a broken
    integration, and the reader cannot tell "not measured" from "zero"."""
    table = adf.table([("Device", "CRAH-01"), ("Serial", ""),
                       ("Asset tag", None)])
    assert _texts(table) == ["Device", "CRAH-01"]


def test_a_table_with_nothing_left_is_no_table_at_all():
    assert adf.table([("Serial", ""), ("Asset tag", "")]) is None


def test_a_header_row_uses_header_cells():
    table = adf.table([("Device", "CRAH-01")], header=("Attribute", "Value"))
    kinds = [c["type"] for row in table["content"] for c in row["content"]]
    assert kinds == ["tableHeader", "tableHeader", "tableCell", "tableCell"]


def test_zero_is_not_dropped():
    """Only empty and None are absent. A measured zero is a real reading - an
    open breaker reports no current, and that is the point."""
    table = adf.table([("Current", "0")])
    assert "0" in _texts(table)


# ------------------------------------------------------------- code blocks

def test_a_long_block_is_truncated_visibly():
    """Visibly, because a create that fails at 09:14 on a Sunday over a
    varbind dump is worse than a ticket that says it left some out."""
    block = adf.code_block("x" * (adf.MAX_CODE_CHARS + 500))
    body = _texts(block)[0]
    assert len(body) < adf.MAX_CODE_CHARS + 200
    assert "truncated" in body and "500 more characters" in body


def test_a_short_block_is_left_alone():
    assert _texts(adf.code_block("{}"))[0] == "{}"


def test_the_json_block_survives_the_three_types_sql_hands_back():
    """datetimes, Decimals and UUIDs - the same three the WebSocket fan-out
    had to learn about. json.dumps refuses all of them, and a TypeError here
    would take down the dispatcher rather than one field."""
    from datetime import UTC, datetime
    from decimal import Decimal
    from uuid import UUID

    payload = {"ts": datetime(2026, 9, 22, tzinfo=UTC),
               "value": Decimal("28.40"),
               "id": UUID("9f1d8a2e-0000-4000-8000-000000000001")}
    body = _texts(adf.json_block(payload))[0]
    assert json.loads(body)["value"] == "28.40"


# --------------------------------------------------------------- flattening

def test_to_text_renders_a_table_row_per_line():
    """Needed because the service desk API has historically taken
    `description` as a plain string while /rest/api/3/issue demands ADF for
    the same field. One document, rendered either way."""
    doc = adf.doc(
        adf.paragraph("Inlet 28.4C above 26.0C"),
        adf.table([("Device", "CRAH-01"), ("Severity", "MAJOR")]),
        adf.rule(),
        adf.code_block("{}"))
    text = adf.to_text(doc)
    assert text.splitlines() == [
        "Inlet 28.4C above 26.0C", "Device\tCRAH-01", "Severity\tMAJOR",
        "---", "{}"]


def test_to_text_joins_a_paragraphs_runs_without_a_break():
    doc = adf.doc(adf.paragraph(adf.text("Cleared.", adf.strong()),
                                adf.text(" Condition no longer met.")))
    assert adf.to_text(doc) == "Cleared. Condition no longer met."
