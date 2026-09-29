// Package mtls is the collector's half of certificate-based enrollment:
// generating a key pair and CSR, exchanging a one-time token for a
// certificate, persisting it, and presenting it on every future connection
// to the platform - renewed automatically before it expires.
//
// A bearer token never touches disk more securely than the YAML file it
// lives in. A private key generated here never leaves this process: only
// its public half, inside a CSR, is ever sent anywhere.
package mtls

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const (
	certFile  = "collector-cert.pem"
	keyFile   = "collector-key.pem"
	chainFile = "collector-chain.pem"

	// Matches the server's ca.CURVE (SECP256R1/P-256): not load-bearing for
	// interop - X.509 does not require a client to match the CA's curve -
	// but there is no reason for the two ends of one system to differ.
	fileMode = 0o600
)

// Store holds the collector's current client certificate and serves it to
// every TLS handshake through GetClientCertificate, which is called fresh
// on each connection - so a renewal that calls Set takes effect on the next
// connection with no restart, no transport rebuild, and no coordination
// with the three packages (assign, config, discovery) that each hold their
// own *http.Client pointed at the same *tls.Config this produces.
type Store struct {
	mu       sync.RWMutex
	cert     *tls.Certificate
	notAfter time.Time
}

func NewStore() *Store { return &Store{} }

func (s *Store) Set(cert tls.Certificate, notAfter time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cert = &cert
	s.notAfter = notAfter
}

// NotAfter is the zero time until a certificate has been loaded or issued.
func (s *Store) NotAfter() time.Time {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.notAfter
}

// Enrolled is false until Set has been called at least once - whether this
// collector has anything to present at all.
func (s *Store) Enrolled() bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.cert != nil
}

// TLSConfig presents whatever certificate is currently in the Store. Nil
// before enrollment: not an error, a connection simply proceeds without a
// client certificate, which is what lets a not-yet-enrolled collector keep
// working on its bearer token exactly as before mTLS existed at all.
func (s *Store) TLSConfig() *tls.Config {
	return &tls.Config{
		GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			s.mu.RLock()
			defer s.mu.RUnlock()
			if s.cert == nil {
				return &tls.Certificate{}, nil
			}
			return s.cert, nil
		},
	}
}

type certResponse struct {
	CertPEM  string    `json:"cert_pem"`
	Chain    []string  `json:"chain"`
	NotAfter time.Time `json:"not_after"`
	Serial   string    `json:"serial"`
}

func generateKey() (*ecdsa.PrivateKey, error) {
	return ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
}

func buildCSR(collectorID string, key *ecdsa.PrivateKey) ([]byte, error) {
	template := x509.CertificateRequest{
		Subject:  pkix.Name{CommonName: collectorID},
		DNSNames: []string{collectorID},
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &template, key)
	if err != nil {
		return nil, fmt.Errorf("build CSR: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}), nil
}

func keyPEM(key *ecdsa.PrivateKey) ([]byte, error) {
	der, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der}), nil
}

func postJSON(ctx context.Context, httpClient *http.Client, url string,
	body any, auth string) (*certResponse, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url,
		bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if auth != "" {
		req.Header.Set("Authorization", auth)
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: HTTP %d: %s", url, resp.StatusCode, string(respBody))
	}
	var out certResponse
	if err := json.Unmarshal(respBody, &out); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}
	return &out, nil
}

// Enroll is the one-shot `collector enroll` action: generate a key pair,
// build a CSR, exchange the one-time token for a certificate, and write
// everything to stateDir. Run once per collector, by a human or a
// provisioning script - not by the long-running collector process, which
// only ever loads what this wrote.
func Enroll(ctx context.Context, baseURL, collectorID, token, stateDir string,
	timeout time.Duration) error {
	key, err := generateKey()
	if err != nil {
		return fmt.Errorf("generate key: %w", err)
	}
	csr, err := buildCSR(collectorID, key)
	if err != nil {
		return err
	}
	httpClient := &http.Client{Timeout: timeout}
	resp, err := postJSON(ctx, httpClient, baseURL+"/api/v1/collector/enroll",
		map[string]string{"token": token, "csr_pem": string(csr)}, "")
	if err != nil {
		return fmt.Errorf("enroll: %w", err)
	}
	return persist(stateDir, key, resp)
}

// Renew presents the current certificate over mTLS (httpClient must already
// be configured with the Store's TLSConfig) and exchanges it, plus a FRESH
// key pair, for a new one. A new key on every renewal costs nothing and
// means a compromised old key stops being useful the moment its certificate
// would have needed renewing anyway.
func Renew(ctx context.Context, baseURL, collectorID, stateDir string,
	httpClient *http.Client) (*certResponse, *ecdsa.PrivateKey, error) {
	key, err := generateKey()
	if err != nil {
		return nil, nil, fmt.Errorf("generate key: %w", err)
	}
	csr, err := buildCSR(collectorID, key)
	if err != nil {
		return nil, nil, err
	}
	resp, err := postJSON(ctx, httpClient, baseURL+"/api/v1/collector/renew",
		map[string]string{"csr_pem": string(csr)}, "")
	if err != nil {
		return nil, nil, fmt.Errorf("renew: %w", err)
	}
	if err := persist(stateDir, key, resp); err != nil {
		return nil, nil, err
	}
	return resp, key, nil
}

// persist writes the leaf, its key, and the full trust chain to stateDir.
//
// certFile holds the leaf FOLLOWED BY every intermediate in resp.Chain - not
// the leaf alone. A TLS client presents its whole path up to (but not
// including) the root in the handshake, because the verifier's trust store
// holds only the root: proved live against a real nginx front door, which
// answered "400 The SSL certificate error" for a leaf presented by itself
// and "SUCCESS" once the intermediate rode along with it in the same file -
// tls.LoadX509KeyPair happily parses a cert file holding more than one
// concatenated PEM block, in order, into the certificate chain it builds.
//
// resp.Chain is root LAST (collector_pki.trust_chain's contract), so
// everything except the last entry is what belongs in certFile; chainFile
// keeps the full chain, root included, for anything that wants it as
// reference rather than as what gets sent.
func persist(stateDir string, key *ecdsa.PrivateKey, resp *certResponse) error {
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return fmt.Errorf("create state dir: %w", err)
	}
	kp, err := keyPEM(key)
	if err != nil {
		return err
	}
	writes := map[string][]byte{
		certFile:  []byte(resp.CertPEM + joinPEM(withoutRoot(resp.Chain))),
		keyFile:   kp,
		chainFile: []byte(joinPEM(resp.Chain)),
	}
	for name, data := range writes {
		if err := os.WriteFile(filepath.Join(stateDir, name), data, fileMode); err != nil {
			return fmt.Errorf("write %s: %w", name, err)
		}
	}
	return nil
}

// chainDER is the leaf plus every intermediate in resp.Chain (root
// excluded, same rule as persist), decoded to DER - the form
// tls.Certificate.Certificate needs. Shared by RenewLoop's in-memory
// update, which has no file to re-read the way Load does.
func chainDER(resp *certResponse) ([][]byte, error) {
	pems := append([]string{resp.CertPEM}, withoutRoot(resp.Chain)...)

	var out [][]byte
	for _, p := range pems {
		block, _ := pem.Decode([]byte(p))
		if block == nil {
			return nil, fmt.Errorf("certificate PEM would not decode")
		}
		out = append(out, block.Bytes)
	}
	return out, nil
}

// withoutRoot drops the last entry of a chain trust_chain returns root-last
// (collector_pki.trust_chain's contract). A client presents everything up
// to but not including the root - the verifier's trust store already holds
// that.
func withoutRoot(chain []string) []string {
	if len(chain) == 0 {
		return chain
	}
	return chain[:len(chain)-1]
}

func joinPEM(certs []string) string {
	out := ""
	for _, c := range certs {
		out += c
	}
	return out
}

// Load reads a previously enrolled certificate and key from stateDir into a
// tls.Certificate, or returns (nil, zero-time, nil) - not an error - if
// nothing has been enrolled yet.
func Load(stateDir string) (*tls.Certificate, time.Time, error) {
	certPath := filepath.Join(stateDir, certFile)
	keyPath := filepath.Join(stateDir, keyFile)
	if _, err := os.Stat(certPath); errors.Is(err, os.ErrNotExist) {
		return nil, time.Time{}, nil
	}
	pair, err := tls.LoadX509KeyPair(certPath, keyPath)
	if err != nil {
		return nil, time.Time{}, fmt.Errorf("load enrolled certificate: %w", err)
	}
	leaf, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		return nil, time.Time{}, fmt.Errorf("parse enrolled certificate: %w", err)
	}
	return &pair, leaf.NotAfter, nil
}

// RenewalDue mirrors the server's collector_pki.renewal_due: due once less
// than a third of the certificate's original lifetime remains.
func RenewalDue(notAfter time.Time, lifetime time.Duration) bool {
	remaining := time.Until(notAfter)
	return remaining <= lifetime/3
}

// RenewLoop checks periodically and renews when RenewalDue, updating store
// (so every in-flight *http.Client picks up the new certificate immediately)
// and disk (so a restart does not lose it). Logs and retries on failure
// rather than crashing the collector - a renewal that fails today still has
// up to a third of the certificate's lifetime to succeed on a later try.
func RenewLoop(ctx context.Context, baseURL, collectorID, stateDir string,
	lifetime, checkEvery time.Duration, store *Store, log *slog.Logger) {
	ticker := time.NewTicker(checkEvery)
	defer ticker.Stop()
	httpClient := &http.Client{Transport: &http.Transport{TLSClientConfig: store.TLSConfig()}}
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if !store.Enrolled() || !RenewalDue(store.NotAfter(), lifetime) {
				continue
			}
			resp, key, err := Renew(ctx, baseURL, collectorID, stateDir, httpClient)
			if err != nil {
				log.Error("certificate renewal failed; will retry",
					"error", err, "not_after", store.NotAfter())
				continue
			}
			// Already persisted to disk inside Renew (via persist); this
			// only has to update what the running transports present next -
			// leaf AND intermediate, the same shape persist() writes to
			// certFile, or a renewed collector goes back to failing the
			// verification proved live against nginx above.
			chain, err := chainDER(resp)
			if err != nil {
				log.Error("renewed certificate chain could not be parsed", "error", err)
				continue
			}
			store.Set(tls.Certificate{
				Certificate: chain,
				PrivateKey:  key,
			}, resp.NotAfter)
			log.Info("certificate renewed", "not_after", resp.NotAfter, "serial", resp.Serial)
		}
	}
}
