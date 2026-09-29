"""The certificate authority's crypto, with no database involved.

docs/26 Phase 2. Every property a client-cert scheme actually depends on:
the chain verifies, a CSR claiming an identity the token was not issued for
is refused, a tampered request is refused, and the renewal threshold is a
pure function of two numbers.
"""

from __future__ import annotations

import datetime

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.services import ca, collector_pki


@pytest.fixture(scope="module")
def pki():
    root = ca.generate_root()
    intermediate = ca.generate_intermediate(root.cert_pem, root.key_pem)
    return root, intermediate


def test_a_leaf_certificate_chains_to_the_root(pki):
    root, intermediate = pki
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-dc2-oob", key)
    issued = ca.sign_collector_csr(
        csr, collector_id="col-dc2-oob",
        intermediate_cert_pem=intermediate.cert_pem,
        intermediate_key_pem=intermediate.key_pem)
    assert ca.verify_chain(issued.cert_pem, intermediate.cert_pem, root.cert_pem)
    assert len(issued.serial) > 0
    assert len(issued.fingerprint_sha256) == 64


def test_the_leaf_is_scoped_to_thirty_days_by_default(pki):
    _root, intermediate = pki
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-x", key)
    issued = ca.sign_collector_csr(
        csr, collector_id="col-x",
        intermediate_cert_pem=intermediate.cert_pem,
        intermediate_key_pem=intermediate.key_pem)
    remaining = issued.not_after - datetime.datetime.now(datetime.UTC)
    assert datetime.timedelta(days=29) < remaining <= datetime.timedelta(days=30)


def test_a_csr_for_a_different_identity_than_the_token_covers_is_refused(pki):
    """The security property enrollment exists to provide: a stolen token
    proves "may enroll as THIS id", never "may enroll as anything"."""
    _root, intermediate = pki
    key = ec.generate_private_key(ca.CURVE)
    csr = ca.build_csr("col-attacker-chosen-name", key)
    with pytest.raises(ca.CAError, match="does not match"):
        ca.sign_collector_csr(
            csr, collector_id="col-the-approved-one",
            intermediate_cert_pem=intermediate.cert_pem,
            intermediate_key_pem=intermediate.key_pem)


def test_a_malformed_csr_is_refused_not_crashed_on(pki):
    _root, intermediate = pki
    with pytest.raises(ca.CAError):
        ca.sign_collector_csr(
            "not a csr", collector_id="col-x",
            intermediate_cert_pem=intermediate.cert_pem,
            intermediate_key_pem=intermediate.key_pem)


def test_two_intermediates_signed_by_the_same_root_both_verify(pki):
    """What lets an intermediate rotate without touching a single collector:
    clients trust the root, and any intermediate the root actually signed
    verifies against it, old or new."""
    root, intermediate_a = pki
    intermediate_b = ca.generate_intermediate(root.cert_pem, root.key_pem)
    key = ec.generate_private_key(ca.CURVE)

    csr_a = ca.build_csr("col-a", key)
    issued_a = ca.sign_collector_csr(
        csr_a, collector_id="col-a",
        intermediate_cert_pem=intermediate_a.cert_pem,
        intermediate_key_pem=intermediate_a.key_pem)
    assert ca.verify_chain(issued_a.cert_pem, intermediate_a.cert_pem, root.cert_pem)

    csr_b = ca.build_csr("col-b", key)
    issued_b = ca.sign_collector_csr(
        csr_b, collector_id="col-b",
        intermediate_cert_pem=intermediate_b.cert_pem,
        intermediate_key_pem=intermediate_b.key_pem)
    assert ca.verify_chain(issued_b.cert_pem, intermediate_b.cert_pem, root.cert_pem)

    # And a's certificate does NOT verify against b's intermediate - a chain
    # is a specific path, not "signed by the CA" in the abstract.
    assert not ca.verify_chain(issued_a.cert_pem, intermediate_b.cert_pem, root.cert_pem)


def test_fingerprint_differs_from_serial():
    """A serial is only unique within its issuer; two different roots could
    mint the same serial number by coincidence. The fingerprint is over the
    whole certificate and cannot collide that way."""
    root = ca.generate_root()
    assert ca.fingerprint(root.cert_pem) != root.serial


# --------------------------------------------------------- renewal timing

def test_renewal_is_not_due_with_most_of_the_lifetime_left():
    not_after = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=25)
    assert not collector_pki.renewal_due(not_after, issued_lifetime_days=30)


def test_renewal_is_due_once_a_third_of_the_lifetime_remains():
    not_after = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=9)
    assert collector_pki.renewal_due(not_after, issued_lifetime_days=30)


def test_renewal_is_due_for_an_already_expired_certificate():
    not_after = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
    assert collector_pki.renewal_due(not_after, issued_lifetime_days=30)
