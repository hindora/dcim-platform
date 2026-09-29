// Package sealedbox is this collector's half of docs/26 Phase 4's
// credential sealing. Before this, GET /collector/assignments decrypted
// every device credential server-side and handed it to whoever asked,
// protected only by TLS in transit. Now the platform seals each credential
// to THIS collector's own public key, so only the process holding the
// matching private key - generated here, never transmitted - can read it,
// regardless of what can see the response body in transit or at rest on
// the platform side.
//
// Unseal is the exact mirror of backend/app/services/sealed_credential.py's
// seal_for_collector - the two must be changed together. This is
// deliberately NOT libsodium/NaCl's crypto_box_seal byte-for-byte (see that
// module's own docstring for why); it has the same anonymous-sender
// security property, built from an ephemeral X25519 keypair, ECDH against
// this collector's long-term public key, HKDF-SHA256, and one AES-256-GCM
// seal. This identity is a separate concern from internal/mtls's TLS
// certificate: it never signs anything and is never presented in a TLS
// handshake, only ever used to receive.
package sealedbox

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/ecdh"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"golang.org/x/crypto/hkdf"
)

const (
	pubKeyBytes = 32
	nonceBytes  = 12
	hkdfInfo    = "dcim-credential-seal-v1"

	keyFile  = "collector-sealedbox-key"
	fileMode = 0o600
)

// KeyPair is one collector's long-term sealing identity.
type KeyPair struct {
	priv *ecdh.PrivateKey
}

// Generate creates a fresh keypair. It is not persisted - call Save, or use
// LoadOrGenerate, which does both.
func Generate() (*KeyPair, error) {
	priv, err := ecdh.X25519().GenerateKey(rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("generate sealedbox keypair: %w", err)
	}
	return &KeyPair{priv: priv}, nil
}

// Load reconstructs a keypair from its raw 32-byte private scalar.
func Load(raw []byte) (*KeyPair, error) {
	priv, err := ecdh.X25519().NewPrivateKey(raw)
	if err != nil {
		return nil, fmt.Errorf("load sealedbox key: %w", err)
	}
	return &KeyPair{priv: priv}, nil
}

// LoadOrGenerate reads the persisted key from stateDir, or generates and
// saves a new one if none exists yet. A collector's sealing key changing
// across a restart would mean the platform is sealing to a public key
// nothing can unseal anymore until the next enrollment - this is the one
// function on the collector's startup path responsible for that never
// happening silently.
func LoadOrGenerate(stateDir string) (*KeyPair, error) {
	path := filepath.Join(stateDir, keyFile)
	raw, err := os.ReadFile(path)
	if err == nil {
		return Load(raw)
	}
	if !os.IsNotExist(err) {
		return nil, fmt.Errorf("read sealedbox key: %w", err)
	}
	kp, genErr := Generate()
	if genErr != nil {
		return nil, genErr
	}
	if err := kp.Save(stateDir); err != nil {
		return nil, err
	}
	return kp, nil
}

// Save persists the raw private scalar to <stateDir>/collector-sealedbox-key,
// 0600, overwriting anything already there.
func (k *KeyPair) Save(stateDir string) error {
	if err := os.MkdirAll(stateDir, 0o700); err != nil {
		return fmt.Errorf("create state dir for sealedbox key: %w", err)
	}
	path := filepath.Join(stateDir, keyFile)
	if err := os.WriteFile(path, k.priv.Bytes(), fileMode); err != nil {
		return fmt.Errorf("write sealedbox key: %w", err)
	}
	return nil
}

// Bytes is the raw 32-byte private scalar.
func (k *KeyPair) Bytes() []byte { return k.priv.Bytes() }

// PublicKeyBytes is the raw 32-byte public key - base64-encode this to get
// the value an enroll request sends as encryption_pubkey.
func (k *KeyPair) PublicKeyBytes() []byte { return k.priv.PublicKey().Bytes() }

// Unseal decrypts blob = ephemeral_pubkey(32) || nonce(12) ||
// ciphertext_with_tag, as produced by seal_for_collector for this
// keypair's public key.
func (k *KeyPair) Unseal(blob []byte) (map[string]any, error) {
	if len(blob) < pubKeyBytes+nonceBytes {
		return nil, fmt.Errorf("sealed credential too short: %d bytes", len(blob))
	}
	ephemeralPubBytes := blob[:pubKeyBytes]
	nonce := blob[pubKeyBytes : pubKeyBytes+nonceBytes]
	ciphertext := blob[pubKeyBytes+nonceBytes:]

	ephemeralPub, err := ecdh.X25519().NewPublicKey(ephemeralPubBytes)
	if err != nil {
		return nil, fmt.Errorf("sealed credential: bad ephemeral public key: %w", err)
	}
	shared, err := k.priv.ECDH(ephemeralPub)
	if err != nil {
		return nil, fmt.Errorf("sealed credential: ECDH failed: %w", err)
	}

	key, err := deriveKey(shared, ephemeralPubBytes, k.PublicKeyBytes())
	if err != nil {
		return nil, err
	}

	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("sealed credential: build AES cipher: %w", err)
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("sealed credential: build GCM: %w", err)
	}
	plaintext, err := gcm.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		return nil, fmt.Errorf("sealed credential: decrypt failed "+
			"(wrong key, or the platform sealed to a stale enrollment): %w", err)
	}

	var payload map[string]any
	if err := json.Unmarshal(plaintext, &payload); err != nil {
		return nil, fmt.Errorf("sealed credential: decode payload: %w", err)
	}
	return payload, nil
}

// deriveKey mirrors sealed_credential.py's _derive_key exactly: HKDF-SHA256
// over the ECDH shared secret, salted with ephemeralPub||recipientPub and
// bound to a fixed info string, both sides changed together.
func deriveKey(shared, ephemeralPub, recipientPub []byte) ([]byte, error) {
	salt := make([]byte, 0, len(ephemeralPub)+len(recipientPub))
	salt = append(salt, ephemeralPub...)
	salt = append(salt, recipientPub...)
	reader := hkdf.New(sha256.New, shared, salt, []byte(hkdfInfo))
	key := make([]byte, 32)
	if _, err := io.ReadFull(reader, key); err != nil {
		return nil, fmt.Errorf("derive sealed-credential key: %w", err)
	}
	return key, nil
}
