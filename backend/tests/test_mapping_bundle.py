"""The platform's mapping fingerprint, checked against its two siblings.

Three implementations of the same algorithm exist: this backend service,
contracts/mapping_bundle.py (the standalone tool contributors run by hand),
and collector/internal/mapping/bundle.go (what a collector actually reports).
They can drift apart silently - each is a separate file, in a separate
language for two of the three - so this test imports both Python copies by
path and hashes the same fixture with each, and a companion Go test
(collector/internal/mapping/bundle_test.go) checks the embedded bundle
against contracts/mappings directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from app.services import mapping_bundle

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_standalone_bundle_sha():
    spec = importlib.util.spec_from_file_location(
        "contracts_mapping_bundle", REPO_ROOT / "contracts" / "mapping_bundle.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.bundle_sha


def test_the_backend_copy_agrees_with_the_standalone_tool(tmp_path):
    (tmp_path / "snmp").mkdir()
    (tmp_path / "snmp" / "standard.yaml").write_text("a: 1\n")
    (tmp_path / "bacnet").mkdir()
    (tmp_path / "bacnet" / "objects.yaml").write_text("b: 2\n")

    standalone_sha = _load_standalone_bundle_sha()
    assert mapping_bundle.bundle_sha(tmp_path) == standalone_sha(tmp_path)


def test_the_backend_copy_reads_the_real_contracts_mappings_directory():
    # Not a fixture: the real directory this platform ships with, exercised
    # end to end so a real file this algorithm cannot handle (a symlink, a
    # permission problem) shows up here rather than only against a fixture.
    assert mapping_bundle.MAPPINGS_DIR.is_dir()
    sha = mapping_bundle.expected_sha()
    assert sha is not None
    assert len(sha) == 64
    # Same object every call: contracts/mappings cannot change within a
    # running process, so re-hashing it is wasted work, not fresh information.
    assert mapping_bundle.expected_sha() is sha


def test_order_of_files_on_disk_does_not_change_the_digest(tmp_path):
    (tmp_path / "z").mkdir()
    (tmp_path / "z" / "last.yaml").write_text("z: 1\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "first.yaml").write_text("a: 1\n")
    sha1 = mapping_bundle.bundle_sha(tmp_path)

    other = tmp_path.parent / "reordered"
    other.mkdir()
    (other / "a").mkdir()
    (other / "a" / "first.yaml").write_text("a: 1\n")
    (other / "z").mkdir()
    (other / "z" / "last.yaml").write_text("z: 1\n")
    sha2 = mapping_bundle.bundle_sha(other)

    assert sha1 == sha2
