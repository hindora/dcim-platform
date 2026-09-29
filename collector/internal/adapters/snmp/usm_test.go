package snmp

import (
	"errors"
	"testing"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

func v3Cred(data map[string]any) *models.Credential {
	return &models.Credential{Kind: "snmp_v3", Data: data}
}

func TestParseUSMAcceptsAValidAuthPrivCredential(t *testing.T) {
	usm, err := parseUSM(v3Cred(map[string]any{
		"security_name": "monitor", "auth_protocol": "sha256", "auth_key": "authpass123",
		"priv_protocol": "aes256", "priv_key": "privpass123",
	}))
	if err != nil {
		t.Fatalf("parseUSM: %v", err)
	}
	if usm.securityName != "monitor" || usm.authProtocol != g.SHA256 ||
		usm.privProtocol != g.AES256 {
		t.Fatalf("parseUSM = %+v", usm)
	}
	if usm.msgFlags() != g.AuthPriv {
		t.Errorf("msgFlags = %v, want AuthPriv", usm.msgFlags())
	}
}

func TestParseUSMAcceptsAuthNoPriv(t *testing.T) {
	usm, err := parseUSM(v3Cred(map[string]any{
		"security_name": "ro-user", "auth_protocol": "sha", "auth_key": "authpass123",
	}))
	if err != nil {
		t.Fatalf("parseUSM: %v", err)
	}
	if usm.msgFlags() != g.AuthNoPriv {
		t.Errorf("msgFlags = %v, want AuthNoPriv", usm.msgFlags())
	}
}

func TestParseUSMAcceptsNoAuthNoPriv(t *testing.T) {
	usm, err := parseUSM(v3Cred(map[string]any{"security_name": "public-v3"}))
	if err != nil {
		t.Fatalf("parseUSM: %v", err)
	}
	if usm.msgFlags() != g.NoAuthNoPriv {
		t.Errorf("msgFlags = %v, want NoAuthNoPriv", usm.msgFlags())
	}
}

func TestParseUSMCaseInsensitiveProtocolNames(t *testing.T) {
	usm, err := parseUSM(v3Cred(map[string]any{
		"security_name": "u", "auth_protocol": "SHA256", "auth_key": "x",
		"priv_protocol": "AES192C", "priv_key": "y",
	}))
	if err != nil {
		t.Fatalf("parseUSM: %v", err)
	}
	if usm.authProtocol != g.SHA256 || usm.privProtocol != g.AES192C {
		t.Fatalf("parseUSM = %+v", usm)
	}
}

func TestParseUSMRejectsMissingSecurityName(t *testing.T) {
	_, err := parseUSM(v3Cred(map[string]any{"auth_protocol": "sha256"}))
	assertConfigError(t, err)
}

func TestParseUSMRejectsUnknownAuthProtocol(t *testing.T) {
	_, err := parseUSM(v3Cred(map[string]any{
		"security_name": "u", "auth_protocol": "sha3-nonsense"}))
	assertConfigError(t, err)
}

func TestParseUSMRejectsUnknownPrivProtocol(t *testing.T) {
	_, err := parseUSM(v3Cred(map[string]any{
		"security_name": "u", "priv_protocol": "blowfish"}))
	assertConfigError(t, err)
}

func TestParseUSMRejectsAuthProtocolWithNoKey(t *testing.T) {
	_, err := parseUSM(v3Cred(map[string]any{
		"security_name": "u", "auth_protocol": "sha256"}))
	assertConfigError(t, err)
}

func TestParseUSMRejectsPrivProtocolWithNoKey(t *testing.T) {
	_, err := parseUSM(v3Cred(map[string]any{
		"security_name": "u", "auth_protocol": "sha256", "auth_key": "x",
		"priv_protocol": "aes256"}))
	assertConfigError(t, err)
}

func TestParseUSMRejectsPrivWithoutAuth(t *testing.T) {
	// RFC 3414 has no privacy-without-authentication security level - USM
	// would otherwise silently downgrade to noAuthNoPriv.
	_, err := parseUSM(v3Cred(map[string]any{
		"security_name": "u", "priv_protocol": "aes256", "priv_key": "y"}))
	assertConfigError(t, err)
}

func TestParseUSMRejectsNilCredential(t *testing.T) {
	_, err := parseUSM(nil)
	assertConfigError(t, err)
}

func assertConfigError(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected an error, got nil")
	}
	if !errors.Is(err, models.ErrConfig) {
		t.Errorf("error = %v, want it to wrap models.ErrConfig", err)
	}
}

func TestIsUSMAuthErrorClassifiesGosnmpSentinels(t *testing.T) {
	for _, err := range []error{g.ErrWrongDigest, g.ErrUnknownUsername,
		g.ErrUnknownSecurityLevel, g.ErrDecryption} {
		if !isUSMAuthError(err) {
			t.Errorf("isUSMAuthError(%v) = false, want true", err)
		}
	}
}

func TestIsUSMAuthErrorRejectsUnrelatedErrors(t *testing.T) {
	if isUSMAuthError(errors.New("request timed out")) {
		t.Error("a plain timeout was classified as a USM auth error")
	}
	if isUSMAuthError(nil) {
		t.Error("nil was classified as a USM auth error")
	}
}

// TestPollWithAnInvalidV3CredentialFailsAsConfigNotTimeout is the same
// shape as TestEmptyCommunityIsAnAuthErrorNotADefault, for the v3 path: a
// credential the operator got wrong must be diagnosable as "fix the
// credential", not "the device is unreachable".
func TestPollWithAnInvalidV3CredentialFailsAsConfigNotTimeout(t *testing.T) {
	a := New(nil, nil, nil, 25, true)
	ep := &models.Endpoint{
		ID: "ep-1", Protocol: "snmp", Address: "10.0.0.1",
		Credential: v3Cred(map[string]any{}), // no security_name
	}
	_, err := a.Poll(nil, ep) //nolint:staticcheck // nil ctx never reached
	if err == nil {
		t.Fatal("poll with an invalid v3 credential succeeded")
	}
	if models.ClassifyError(err) != models.ErrClassConfig {
		t.Fatalf("error class %q, want config", models.ClassifyError(err))
	}
}
