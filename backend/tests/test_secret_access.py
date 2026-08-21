"""Which code is allowed to touch an encrypted secret at all.

The other redaction tests check that secrets do not escape through a particular
door. This one checks how many doors exist, because the cheapest way to keep a
credential out of a response is for the code that builds responses never to
load it in the first place.

It reads the source with ``ast`` rather than grepping, because several of these
modules explain in prose exactly why they must not read the encrypted column,
and a guard that cannot tell an explanation from an access is a guard that gets
switched off the first time it cries wolf.
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

# The one chain that must decrypt: the collector has to authenticate to devices,
# so the assignments builder reads the column, decrypts it, and hands it to a
# collector-scoped, audited endpoint. Everything else is a bug in waiting.
ALLOWED_SECRET_READERS = {
    "repositories/collector.py",   # SELECTs the column
    "services/collector.py",       # decrypts it into an assignment
    "importer/simulator.py",       # writes it at import time
    "models/endpoints.py",         # declares it
    "core/security.py",            # encrypt/decrypt themselves
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of the string constants that are docstrings, so prose can be ignored."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def mentions_in_code(path: pathlib.Path, needle: str) -> bool:
    """True if the file references the name in code or in SQL, not in prose.

    SQL lives in ordinary string literals - that IS the usage - so string
    constants count, except the ones that are docstrings. Comments never count.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - the app must parse
        return needle in source

    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings and needle in node.value):
            return True
        # Identifier-shaped references: a bare name, an attribute, a keyword
        # argument. Kept as one membership test rather than three branches,
        # which is both shorter and what the linter wanted.
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.keyword):
            name = node.arg
        if name and needle in name:
            return True
    return False


def test_only_the_assignment_chain_reads_the_encrypted_column():
    offenders = sorted(
        p.relative_to(APP).as_posix()
        for p in APP.rglob("*.py")
        if p.relative_to(APP).as_posix() not in ALLOWED_SECRET_READERS
        and mentions_in_code(p, "secret_enc")
    )
    assert offenders == [], f"these read the encrypted secret: {offenders}"


def test_no_request_handler_decrypts_a_secret():
    """Decryption belongs in the service that builds an assignment, never in a
    handler, where the result is one `return` away from the wire."""
    api = APP / "api"
    offenders = sorted(
        p.relative_to(APP).as_posix()
        for p in api.rglob("*.py")
        if mentions_in_code(p, "decrypt_secret")
    )
    assert offenders == []


def test_the_guard_can_tell_prose_from_access(tmp_path):
    """The guard's own failure mode, pinned.

    core/audit.py explains in its docstring why a ciphertext must never reach
    an audit row. A substring grep flags that file, the flag is a false alarm,
    and the usual response is to add the file to an allowlist - which would
    then hide a real access if one ever appeared there.
    """
    prose = tmp_path / "prose.py"
    prose.write_text('"""We must never write secret_enc here."""\nx = 1\n',
                     encoding="utf-8")
    assert not mentions_in_code(prose, "secret_enc")

    sql = tmp_path / "sql.py"
    sql.write_text('q = "SELECT secret_enc FROM credential"\n', encoding="utf-8")
    assert mentions_in_code(sql, "secret_enc")

    comment = tmp_path / "comment.py"
    comment.write_text("# secret_enc is deliberately not read here\nx = 1\n",
                       encoding="utf-8")
    assert not mentions_in_code(comment, "secret_enc")
