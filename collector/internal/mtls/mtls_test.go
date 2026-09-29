package mtls

import (
	"context"
	"crypto/ecdsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// selfSignedCertPEM signs its own throwaway key when the test only needs a
// well-formed certificate to exist (a chain entry, an unrelated party's
// cert). Persisting it alongside a DIFFERENT private key - as
// TestPersistThenLoadRoundTrips does for the leaf under test - needs
// selfSignedCertPEMWithKey instead, or tls.LoadX509KeyPair correctly
// reports the mismatch.
func selfSignedCertPEM(t *testing.T, cn string, notAfter time.Time) string {
	t.Helper()
	key, err := generateKey()
	if err != nil {
		t.Fatal(err)
	}
	return selfSignedCertPEMWithKey(t, cn, notAfter, key)
}

func selfSignedCertPEMWithKey(t *testing.T, cn string, notAfter time.Time,
	key *ecdsa.PrivateKey) string {
	t.Helper()
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: cn},
		NotBefore:    time.Now().Add(-time.Minute),
		NotAfter:     notAfter,
		DNSNames:     []string{cn},
	}
	der, err := x509.CreateCertificate(nil, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
}

// signCSRPEM stands in for the platform's CA: it parses the CSR the client
// actually sent and signs a certificate over THAT public key, the same
// property the real backend's ca.sign_collector_csr provides. Signing a
// certificate over some unrelated key, as an earlier version of this file
// did, produces a cert/key pair that fails to load for the same reason a
// real mismatched issuance would.
func signCSRPEM(t *testing.T, csrPEM string, notAfter time.Time) string {
	t.Helper()
	block, _ := pem.Decode([]byte(csrPEM))
	if block == nil {
		t.Fatal("test CSR did not decode as PEM")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	issuerKey, err := generateKey()
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      csr.Subject,
		NotBefore:    time.Now().Add(-time.Minute),
		NotAfter:     notAfter,
		DNSNames:     csr.DNSNames,
	}
	issuer := &x509.Certificate{SerialNumber: big.NewInt(1),
		Subject: pkix.Name{CommonName: "test issuer"}}
	der, err := x509.CreateCertificate(nil, tmpl, issuer, csr.PublicKey, issuerKey)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
}

// --- pure functions ---------------------------------------------------

func TestRenewalDueOnceAThirdOfTheLifetimeRemains(t *testing.T) {
	lifetime := 30 * 24 * time.Hour
	if RenewalDue(time.Now().Add(20*24*time.Hour), lifetime) {
		t.Error("should not be due with two thirds of its life left")
	}
	if !RenewalDue(time.Now().Add(9*24*time.Hour), lifetime) {
		t.Error("should be due with under a third left")
	}
	if !RenewalDue(time.Now().Add(-time.Hour), lifetime) {
		t.Error("an already-expired certificate is always due")
	}
}

func TestBuildCSRIsSelfConsistentAndCarriesTheCollectorID(t *testing.T) {
	key, err := generateKey()
	if err != nil {
		t.Fatal(err)
	}
	csrPEM, err := buildCSR("col-dc2-oob", key)
	if err != nil {
		t.Fatal(err)
	}
	block, _ := pem.Decode(csrPEM)
	if block == nil {
		t.Fatal("CSR did not decode as PEM")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	if err := csr.CheckSignature(); err != nil {
		t.Fatalf("CSR signature does not verify against its own public key: %v", err)
	}
	if csr.Subject.CommonName != "col-dc2-oob" {
		t.Errorf("CN = %q", csr.Subject.CommonName)
	}
	if len(csr.DNSNames) != 1 || csr.DNSNames[0] != "col-dc2-oob" {
		t.Errorf("SAN = %v", csr.DNSNames)
	}
}

// --- persist / Load round trip -----------------------------------------

func TestLoadWithNothingEnrolledIsNilNotAnError(t *testing.T) {
	cert, notAfter, err := Load(t.TempDir())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cert != nil {
		t.Fatal("expected no certificate")
	}
	if !notAfter.IsZero() {
		t.Fatal("expected the zero time")
	}
}

func TestPersistThenLoadRoundTrips(t *testing.T) {
	dir := t.TempDir()
	key, err := generateKey()
	if err != nil {
		t.Fatal(err)
	}
	notAfter := time.Now().Add(30 * 24 * time.Hour).Truncate(time.Second)
	resp := &certResponse{
		CertPEM:  selfSignedCertPEMWithKey(t, "col-test", notAfter, key),
		Chain:    []string{selfSignedCertPEM(t, "DCIM Root", time.Now().Add(3650*24*time.Hour))},
		NotAfter: notAfter,
		Serial:   "abc123",
	}
	if err := persist(dir, key, resp); err != nil {
		t.Fatal(err)
	}

	for _, name := range []string{certFile, keyFile, chainFile} {
		info, err := os.Stat(filepath.Join(dir, name))
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if runtime := info.Mode().Perm(); runtime != fileMode && !isWindows() {
			t.Errorf("%s: mode %v, want %v", name, runtime, fileMode)
		}
	}

	cert, loadedNotAfter, err := Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	if cert == nil {
		t.Fatal("expected a certificate")
	}
	if !loadedNotAfter.Equal(notAfter) {
		t.Errorf("not_after = %v, want %v", loadedNotAfter, notAfter)
	}
}

func isWindows() bool { return os.PathSeparator == '\\' }

// TestCertFileCarriesTheIntermediateNotJustTheLeaf pins down a bug an actual
// nginx front door caught live: a leaf presented alone cannot be verified by
// a proxy whose trust store holds only the root, and nginx failed the TLS
// handshake outright ("400 The SSL certificate error") rather than a soft
// FAILED verdict. certFile has to carry the leaf followed by every
// intermediate for tls.LoadX509KeyPair to build the chain a client actually
// needs to present.
func TestCertFileCarriesTheIntermediateNotJustTheLeaf(t *testing.T) {
	dir := t.TempDir()
	key, err := generateKey()
	if err != nil {
		t.Fatal(err)
	}
	leaf := selfSignedCertPEMWithKey(t, "col-test", time.Now().Add(30*24*time.Hour), key)
	intermediate := selfSignedCertPEM(t, "DCIM Issuing CA", time.Now().Add(730*24*time.Hour))
	root := selfSignedCertPEM(t, "DCIM Root", time.Now().Add(3650*24*time.Hour))
	resp := &certResponse{
		CertPEM:  leaf,
		Chain:    []string{intermediate, root}, // root last, per trust_chain's contract
		NotAfter: time.Now().Add(30 * 24 * time.Hour),
		Serial:   "abc",
	}
	if err := persist(dir, key, resp); err != nil {
		t.Fatal(err)
	}

	raw, err := os.ReadFile(filepath.Join(dir, certFile))
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Count(string(raw), "BEGIN CERTIFICATE"); got != 2 {
		t.Fatalf("certFile holds %d certificates, want 2 (leaf + intermediate)", got)
	}
	// PEM is base64: a plaintext substring search on the file cannot find a
	// name inside it. Parse the second block and check its actual subject.
	rest := raw
	block, rest := pem.Decode(rest)
	block, rest = pem.Decode(rest)
	if block == nil {
		t.Fatal("certFile's second PEM block did not decode")
	}
	second, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	if second.Subject.CommonName != "DCIM Issuing CA" {
		t.Errorf("certFile's second certificate is %q, want the intermediate",
			second.Subject.CommonName)
	}
	if third, _ := pem.Decode(rest); third != nil {
		t.Error("certFile carries a third certificate - it should stop at the intermediate, no root")
	}

	pair, _, err := Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(pair.Certificate) != 2 {
		t.Fatalf("loaded chain has %d entries, want 2 (leaf + intermediate)", len(pair.Certificate))
	}

	der, err := chainDER(resp)
	if err != nil {
		t.Fatal(err)
	}
	if len(der) != 2 {
		t.Fatalf("chainDER returned %d entries, want 2 (leaf + intermediate)", len(der))
	}
}

// --- wire format, against a fake server ---------------------------------

func TestEnrollSendsTheTokenAndCSRAndPersistsTheResult(t *testing.T) {
	var gotBody map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/collector/enroll" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		notAfter := time.Now().Add(30 * 24 * time.Hour)
		resp := certResponse{
			CertPEM:  signCSRPEM(t, gotBody["csr_pem"], notAfter),
			Chain:    []string{selfSignedCertPEM(t, "DCIM Root", time.Now().Add(3650*24*time.Hour))},
			NotAfter: notAfter,
			Serial:   "0102",
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}))
	defer server.Close()

	dir := t.TempDir()
	err := Enroll(context.Background(), server.URL, "col-test-enroll",
		"one-time-token", dir, "fake-sealedbox-pubkey-b64", 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if gotBody["token"] != "one-time-token" {
		t.Errorf("token sent = %q", gotBody["token"])
	}
	if gotBody["encryption_pubkey"] != "fake-sealedbox-pubkey-b64" {
		t.Errorf("encryption_pubkey sent = %q, want the sealing key's public half",
			gotBody["encryption_pubkey"])
	}
	if gotBody["csr_pem"] == "" {
		t.Error("no CSR sent")
	}

	cert, notAfter, err := Load(dir)
	if err != nil {
		t.Fatal(err)
	}
	if cert == nil || notAfter.IsZero() {
		t.Fatal("enrollment did not leave a usable certificate on disk")
	}
}

func TestEnrollWithNoSealedboxKeyOmitsTheFieldEntirely(t *testing.T) {
	var gotBody map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
		notAfter := time.Now().Add(30 * 24 * time.Hour)
		resp := certResponse{
			CertPEM:  signCSRPEM(t, gotBody["csr_pem"].(string), notAfter),
			Chain:    []string{selfSignedCertPEM(t, "DCIM Root", time.Now().Add(3650*24*time.Hour))},
			NotAfter: notAfter,
			Serial:   "0104",
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}))
	defer server.Close()

	dir := t.TempDir()
	if err := Enroll(context.Background(), server.URL, "col-no-seal", "tok", dir,
		"", 5*time.Second); err != nil {
		t.Fatal(err)
	}
	if _, present := gotBody["encryption_pubkey"]; present {
		t.Errorf("encryption_pubkey should be absent from the request body "+
			"when Enroll is called with \"\", got %v", gotBody["encryption_pubkey"])
	}
}

func TestRenewPresentsTheCurrentCertificateOverMTLS(t *testing.T) {
	var sawClientCert bool
	server := httptest.NewUnstartedServer(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			sawClientCert = r.TLS != nil && len(r.TLS.PeerCertificates) > 0
			var body map[string]string
			_ = json.NewDecoder(r.Body).Decode(&body)
			notAfter := time.Now().Add(30 * 24 * time.Hour)
			resp := certResponse{
				CertPEM:  signCSRPEM(t, body["csr_pem"], notAfter),
				Chain:    []string{selfSignedCertPEM(t, "DCIM Root", time.Now().Add(3650*24*time.Hour))},
				NotAfter: notAfter,
				Serial:   "0203",
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(resp)
		}))
	server.TLS = &tls.Config{ClientAuth: tls.RequestClientCert}
	server.StartTLS()
	defer server.Close()

	dir := t.TempDir()
	// A certificate this test presents as ITS client cert - what a real
	// collector would already hold from a previous enroll. Signed over the
	// SAME key that gets persisted, or Load fails the same mismatch check a
	// real enrollment would.
	key, err := generateKey()
	if err != nil {
		t.Fatal(err)
	}
	presented := selfSignedCertPEMWithKey(t, "col-test-renew", time.Now().Add(24*time.Hour), key)
	if err := persist(dir, key, &certResponse{CertPEM: presented, Chain: nil,
		NotAfter: time.Now().Add(24 * time.Hour), Serial: "orig"}); err != nil {
		t.Fatal(err)
	}
	pair, _, err := Load(dir)
	if err != nil || pair == nil {
		t.Fatalf("could not load the certificate this test just wrote: %v", err)
	}

	client := server.Client()
	client.Transport.(*http.Transport).TLSClientConfig.Certificates = []tls.Certificate{*pair}

	_, newKey, err := Renew(context.Background(), server.URL, "col-test-renew", dir, client)
	if err != nil {
		t.Fatal(err)
	}
	if newKey == nil {
		t.Fatal("renewal did not return the new key")
	}
	if !sawClientCert {
		t.Error("the server never saw a client certificate on the renewal request")
	}
}

// --- Store ---------------------------------------------------------------

func TestStoreServesNoCertificateUntilSet(t *testing.T) {
	s := NewStore()
	if s.Enrolled() {
		t.Fatal("a fresh store reports enrolled")
	}
	cfg := s.TLSConfig()
	cert, err := cfg.GetClientCertificate(&tls.CertificateRequestInfo{})
	if err != nil {
		t.Fatal(err)
	}
	if len(cert.Certificate) != 0 {
		t.Fatal("expected an empty certificate before Set")
	}
}

func TestStoreServesWhatWasSet(t *testing.T) {
	s := NewStore()
	notAfter := time.Now().Add(30 * 24 * time.Hour)
	s.Set(tls.Certificate{Certificate: [][]byte{[]byte("fake-der")}}, notAfter)
	if !s.Enrolled() {
		t.Fatal("expected enrolled after Set")
	}
	if !s.NotAfter().Equal(notAfter) {
		t.Errorf("NotAfter = %v, want %v", s.NotAfter(), notAfter)
	}
	cfg := s.TLSConfig()
	cert, err := cfg.GetClientCertificate(&tls.CertificateRequestInfo{})
	if err != nil {
		t.Fatal(err)
	}
	if string(cert.Certificate[0]) != "fake-der" {
		t.Error("TLSConfig did not serve the certificate that was Set")
	}
}
