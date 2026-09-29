"""The internal certificate authority that identifies collectors.

Two tiers, both generated here, kept apart by where their private keys live
afterwards:

- **Root.** Self-signed, long-lived, `path_length=1` (it may sign exactly one
  layer of intermediate and nothing else). Its private key is written to a
  file and this process never touches that file again after bootstrap - see
  ``bootstrap()``. An operator is told to move it offline. Every collector
  and every proxy is configured to trust the root, never an intermediate
  directly, which is what lets the intermediate rotate without touching a
  single collector's config.
- **Intermediate.** Signed by the root, shorter-lived, `path_length=0` (it
  may sign leaf certificates and nothing else - not a further intermediate).
  Its private key IS kept here, sealed with ``DCIM_CREDENTIAL_KEY`` in
  ``certificate_authority.key_enc``, because an intermediate has to be online
  to answer an enrollment on demand. This is what actually signs a
  collector's certificate.

A collector's own private key is never generated or held here at all. It
generates its own key pair locally and sends only a CSR - a public key plus a
self-signed proof it holds the matching private key - which is everything
``sign_collector_csr`` needs to issue a certificate without ever seeing the
secret half.

EC/P-256 throughout: broadly supported, fast to generate and verify, and
there is no reason for a short-lived leaf certificate to carry an RSA key's
larger footprint.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CURVE = ec.SECP256R1()

ROOT_LIFETIME_DAYS = 3650  # 10 years - generated once, kept offline.
INTERMEDIATE_LIFETIME_DAYS = 730  # 2 years - rotated well ahead of that.
DEFAULT_LEAF_LIFETIME_DAYS = 30  # docs/26's judgment call; no vendor publishes one.


class CAError(Exception):
    """A CSR or certificate failed a check that must stop the enrollment."""


@dataclass
class Generated:
    cert_pem: str
    key_pem: str
    serial: str  # lowercase hex, no colons - what we index and compare on
    not_after: datetime.datetime


@dataclass
class IssuedCert:
    cert_pem: str
    serial: str
    fingerprint_sha256: str
    not_after: datetime.datetime


def _serial_hex(cert: x509.Certificate) -> str:
    return format(cert.serial_number, "x")


def _key_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _cert_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def generate_root(common_name: str = "DCIM Platform Root CA") -> Generated:
    """A new, self-signed root. Call this once per platform, ever.

    Rerunning it later mints a SECOND root that nothing trusts yet - rotating
    the trusted root is a fleet-wide reconfiguration (every collector and
    every proxy's trust bundle), deliberately not something this function
    does by itself.
    """
    key = ec.generate_private_key(CURVE)
    now = datetime.datetime.now(datetime.UTC)
    not_after = now + datetime.timedelta(days=ROOT_LIFETIME_DAYS)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    return Generated(_cert_pem(cert), _key_pem(key), _serial_hex(cert), not_after)


def generate_intermediate(root_cert_pem: str, root_key_pem: str,
                          common_name: str = "DCIM Platform Issuing CA") -> Generated:
    """A new intermediate, signed by the root. Safe to call again to rotate:
    the result is not activated here - see ``activate_intermediate``."""
    root_cert = x509.load_pem_x509_certificate(root_cert_pem.encode())
    root_key = serialization.load_pem_private_key(root_key_pem.encode(), password=None)

    key = ec.generate_private_key(CURVE)
    now = datetime.datetime.now(datetime.UTC)
    not_after = now + datetime.timedelta(days=INTERMEDIATE_LIFETIME_DAYS)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    return Generated(_cert_pem(cert), _key_pem(key), _serial_hex(cert), not_after)


def build_csr(collector_id: str, key: ec.EllipticCurvePrivateKey) -> str:
    """For tests, and as the reference the Go client's CSR must match.

    A real collector builds its own CSR (crypto/x509 in Go); this exists so
    Python tests can produce one without a Go toolchain, and so both sides
    are provably building the same shape of request.
    """
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, collector_id)]))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(collector_id)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def sign_collector_csr(csr_pem: str, *, collector_id: str,
                       intermediate_cert_pem: str, intermediate_key_pem: str,
                       lifetime_days: int = DEFAULT_LEAF_LIFETIME_DAYS) -> IssuedCert:
    """Turn a CSR into a client certificate scoped to exactly one collector.

    Two checks stand between an enrollment token and an arbitrary identity:
    the CSR's own signature must verify (it must actually be signed by the
    private key behind its public key, not merely well-formed), and its CN
    must equal the collector id the token was issued for - a stolen token
    proves nothing beyond "may enroll as THIS id", never "may enroll as
    anything".
    """
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
    except ValueError as exc:
        raise CAError(f"malformed CSR: {exc}") from None
    if not csr.is_signature_valid:
        raise CAError("CSR signature does not verify against its own public key")

    cn_attrs = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    csr_cn = cn_attrs[0].value if cn_attrs else None
    if csr_cn != collector_id:
        raise CAError(
            f"CSR common name {csr_cn!r} does not match the enrolling "
            f"collector {collector_id!r}")

    intermediate_cert = x509.load_pem_x509_certificate(intermediate_cert_pem.encode())
    intermediate_key = serialization.load_pem_private_key(
        intermediate_key_pem.encode(), password=None)

    now = datetime.datetime.now(datetime.UTC)
    not_after = now + datetime.timedelta(days=lifetime_days)
    cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject).issuer_name(intermediate_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=True,
            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.CLIENT_AUTH]),
                       critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(collector_id)]),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            intermediate_key.public_key()), critical=False)
        .sign(intermediate_key, hashes.SHA256())
    )
    return IssuedCert(_cert_pem(cert), _serial_hex(cert), fingerprint(cert), not_after)


def fingerprint(cert: x509.Certificate | str) -> str:
    """sha256 of the certificate's DER encoding - what a serial cannot give
    you, because a serial is only unique within one issuer."""
    if isinstance(cert, str):
        cert = x509.load_pem_x509_certificate(cert.encode())
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def verify_chain(cert_pem: str, intermediate_cert_pem: str, root_cert_pem: str) -> bool:
    """True if cert -> intermediate -> root is a valid signature chain.

    Not what a TLS handshake uses in production (the proxy's own TLS stack
    does that, against the root's cert file directly) - this is for the
    enrollment endpoint's own sanity check and for tests, so a bug in this
    module fails loudly here rather than as a mystifying handshake error two
    processes away.
    """
    leaf = x509.load_pem_x509_certificate(cert_pem.encode())
    intermediate = x509.load_pem_x509_certificate(intermediate_cert_pem.encode())
    root = x509.load_pem_x509_certificate(root_cert_pem.encode())
    try:
        intermediate.public_key().verify(
            leaf.signature, leaf.tbs_certificate_bytes,
            ec.ECDSA(leaf.signature_hash_algorithm))
        root.public_key().verify(
            intermediate.signature, intermediate.tbs_certificate_bytes,
            ec.ECDSA(intermediate.signature_hash_algorithm))
    except Exception:
        return False
    return True
