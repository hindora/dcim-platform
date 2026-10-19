package redfish

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Action POSTs a Redfish action to the endpoint's ComputerSystem, for example
// Actions/ComputerSystem.Reset with {"ResetType": "GracefulRestart"}.
//
// Like SetInteger on the SNMP side this is the collector's one write path, used
// only for a device action the platform has queued. It reuses the endpoint's
// session, renews it once if the BMC rejects the token (exactly as a poll
// does), and says what the BMC answered when it refuses: a Redfish error body
// carries the reason in plain words, and "returned 400" tells the operator
// nothing.
func (a *Adapter) Action(ctx context.Context, ep *models.Endpoint, actionPath string,
	body map[string]any) error {
	s, err := a.ensureSession(ctx, ep)
	if err != nil {
		return err
	}
	if s.system == "" {
		return fmt.Errorf("%w: no ComputerSystem was discovered on this BMC", models.ErrConfig)
	}
	target := strings.TrimRight(s.system, "/") + "/" + strings.TrimLeft(actionPath, "/")

	err = a.postAction(ctx, ep, s, target, body)
	if err == nil || !errors.Is(err, models.ErrAuth) {
		return err
	}
	// The token died for an ordinary reason. Renew once and ask again; a
	// credential that is actually wrong must still surface as an auth error.
	if authErr := a.authenticate(ctx, ep, s); authErr != nil {
		return authErr
	}
	a.mu.Lock()
	a.sessions[ep.ID] = s
	a.mu.Unlock()
	return a.postAction(ctx, ep, s, target, body)
}

func (a *Adapter) postAction(ctx context.Context, ep *models.Endpoint, s *session, path string,
	body map[string]any) error {
	payload, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.url(ep, s, path),
		bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if s.token != "" {
		req.Header.Set("X-Auth-Token", s.token)
	}
	resp, err := a.client(s.verifyTLS, 30*time.Second).Do(req)
	if err != nil {
		return fmt.Errorf("%w: %v", models.ErrUnreachable, err)
	}
	defer func() { _, _ = io.Copy(io.Discard, resp.Body); resp.Body.Close() }()

	switch {
	case resp.StatusCode == http.StatusUnauthorized:
		a.Forget(ep.ID)
		return fmt.Errorf("%w: token rejected", models.ErrAuth)
	case resp.StatusCode == http.StatusForbidden:
		return fmt.Errorf("%w: the BMC refused the action: this account may not do it",
			models.ErrAuth)
	case resp.StatusCode >= 300:
		return fmt.Errorf("%w: the BMC answered %d: %s", models.ErrProtocolStatus,
			resp.StatusCode, errorMessage(resp.Body))
	}
	return nil
}

// errorMessage pulls the human sentence out of a Redfish error body, falling
// back to the raw text, and never more than a line of it.
func errorMessage(r io.Reader) string {
	raw, _ := io.ReadAll(io.LimitReader(r, 4096))
	var e struct {
		Error struct {
			Message string `json:"message"`
			Info    []struct {
				Message string `json:"Message"`
			} `json:"@Message.ExtendedInfo"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &e) == nil {
		if len(e.Error.Info) > 0 && e.Error.Info[0].Message != "" {
			return e.Error.Info[0].Message
		}
		if e.Error.Message != "" {
			return e.Error.Message
		}
	}
	text := strings.TrimSpace(string(raw))
	if len(text) > 200 {
		text = text[:200]
	}
	if text == "" {
		return "no detail"
	}
	return text
}
