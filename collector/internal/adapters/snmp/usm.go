package snmp

import (
	"errors"
	"fmt"
	"strings"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// usmParams is what a credential of kind "snmp_v3" carries in its Data map
// (docs/26 Phase 4). authKey/privKey are passphrases, not pre-localized
// keys: gosnmp localizes them per RFC 3414 against each agent's own engine
// ID, discovered automatically on Connect - this collector never needs to
// know or store a device's engine ID for polling.
type usmParams struct {
	securityName string
	authProtocol g.SnmpV3AuthProtocol
	authKey      string
	privProtocol g.SnmpV3PrivProtocol
	privKey      string
}

// authProtocolByName / privProtocolByName are the wire names a "snmp_v3"
// credential's auth_protocol/priv_protocol fields use.
//
// SHA-2 (RFC 7860: sha224/sha256/sha384/sha512) and AES-128 (RFC 3826: aes/
// aes128) are the two standards this plan actually asks for. AES-192/256
// are included too, under BOTH variants gosnmp implements - Blumenthal's
// original draft (aes192/aes256) and Reeder's later, more common one
// (aes192c/aes256c) - because several real vendors, Cisco among them, ship
// one or the other despite neither ever being standardized by the IETF.
// Picking between them for a given device is a deployment's decision, not
// something this collector can infer.
var authProtocolByName = map[string]g.SnmpV3AuthProtocol{
	"": g.NoAuth, "none": g.NoAuth,
	"md5": g.MD5, "sha": g.SHA, "sha1": g.SHA,
	"sha224": g.SHA224, "sha256": g.SHA256, "sha384": g.SHA384, "sha512": g.SHA512,
}

var privProtocolByName = map[string]g.SnmpV3PrivProtocol{
	"": g.NoPriv, "none": g.NoPriv,
	"des": g.DES, "aes": g.AES, "aes128": g.AES,
	"aes192": g.AES192, "aes256": g.AES256, // Blumenthal draft - vendor extension
	"aes192c": g.AES192C, "aes256c": g.AES256C, // Reeder variant - vendor extension
}

func parseUSM(cred *models.Credential) (usmParams, error) {
	if cred == nil || cred.Data == nil {
		return usmParams{}, fmt.Errorf("%w: no v3 credential data", models.ErrConfig)
	}
	securityName, _ := cred.Data["security_name"].(string)
	if securityName == "" {
		return usmParams{}, fmt.Errorf("%w: snmp_v3 credential has no security_name",
			models.ErrConfig)
	}
	authName, _ := cred.Data["auth_protocol"].(string)
	authProto, ok := authProtocolByName[strings.ToLower(authName)]
	if !ok {
		return usmParams{}, fmt.Errorf("%w: unknown auth_protocol %q",
			models.ErrConfig, authName)
	}
	privName, _ := cred.Data["priv_protocol"].(string)
	privProto, ok := privProtocolByName[strings.ToLower(privName)]
	if !ok {
		return usmParams{}, fmt.Errorf("%w: unknown priv_protocol %q",
			models.ErrConfig, privName)
	}
	authKey, _ := cred.Data["auth_key"].(string)
	privKey, _ := cred.Data["priv_key"].(string)
	if authProto != g.NoAuth && authKey == "" {
		return usmParams{}, fmt.Errorf("%w: auth_protocol set with no auth_key",
			models.ErrConfig)
	}
	if privProto != g.NoPriv && privKey == "" {
		return usmParams{}, fmt.Errorf("%w: priv_protocol set with no priv_key",
			models.ErrConfig)
	}
	// authPriv needs auth - RFC 3414 has no privacy-without-authentication
	// security level, and USM would otherwise silently downgrade to
	// noAuthNoPriv the moment an operator fills in a priv key but forgets
	// the auth one.
	if privProto != g.NoPriv && authProto == g.NoAuth {
		return usmParams{}, fmt.Errorf(
			"%w: priv_protocol set but auth_protocol is none - USM has no privacy "+
				"without authentication", models.ErrConfig)
	}
	return usmParams{securityName: securityName, authProtocol: authProto, authKey: authKey,
		privProtocol: privProto, privKey: privKey}, nil
}

func (p usmParams) msgFlags() g.SnmpV3MsgFlags {
	switch {
	case p.privProtocol != g.NoPriv:
		return g.AuthPriv
	case p.authProtocol != g.NoAuth:
		return g.AuthNoPriv
	default:
		return g.NoAuthNoPriv
	}
}

// isUSMAuthError reports whether err is one of gosnmp's own USM report-PDU
// sentinels for a security failure - wrong key, unknown security name, a
// security level the agent refuses, or a privacy decrypt failure. The
// agent ANSWERED, and answered that this collector's credential is wrong -
// a materially different fault from a timeout, and docs/26 Phase 4's
// acceptance bar ("a wrong v3 key produces an explicit auth-failure
// endpoint state, not a timeout") is this function existing at all.
func isUSMAuthError(err error) bool {
	return errors.Is(err, g.ErrWrongDigest) || errors.Is(err, g.ErrUnknownUsername) ||
		errors.Is(err, g.ErrUnknownSecurityLevel) || errors.Is(err, g.ErrDecryption)
}
