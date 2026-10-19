package redfish

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// resetBMC is the fake BMC plus the ComputerSystem.Reset action.
type resetBMC struct {
	mu       sync.Mutex
	fake     fakeBMC
	body     string
	resets   int
	reject   int    // answer 401 this many times first
	status   int    // then this status (0 = 204)
	response string // and this body
}

func (b *resetBMC) server(t *testing.T) *httptest.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
		func(w http.ResponseWriter, r *http.Request) {
			b.mu.Lock()
			defer b.mu.Unlock()
			if b.reject > 0 {
				b.reject--
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			raw, _ := io.ReadAll(r.Body)
			b.body = string(raw)
			if b.status != 0 && b.status != http.StatusNoContent {
				w.WriteHeader(b.status)
				_, _ = io.WriteString(w, b.response)
				return
			}
			b.resets++
			w.WriteHeader(http.StatusNoContent)
		})
	mux.Handle("/", b.fake.handler())
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func TestActionPostsTheResetToTheSystem(t *testing.T) {
	bmc := &resetBMC{}
	srv := bmc.server(t)
	a, ep := newAdapter(t), endpointFor(t, srv)
	err := a.Action(context.Background(), ep, "Actions/ComputerSystem.Reset",
		map[string]any{"ResetType": "GracefulRestart"})
	if err != nil {
		t.Fatalf("action failed: %v", err)
	}
	if bmc.resets != 1 || !strings.Contains(bmc.body, `"ResetType":"GracefulRestart"`) {
		t.Fatalf("the BMC saw %d resets, body %q", bmc.resets, bmc.body)
	}
}

func TestARefusalCarriesTheBMCsOwnSentence(t *testing.T) {
	bmc := &resetBMC{status: 400, response: `{"error":{"code":"Base.1.0.GeneralError",
		"message":"A general error has occurred.",
		"@Message.ExtendedInfo":[{"Message":"The value Nuke for ResetType is not valid."}]}}`}
	srv := bmc.server(t)
	err := newAdapter(t).Action(context.Background(), endpointFor(t, srv),
		"Actions/ComputerSystem.Reset", map[string]any{"ResetType": "Nuke"})
	if err == nil || !errors.Is(err, models.ErrProtocolStatus) {
		t.Fatalf("want a protocol error, got %v", err)
	}
	if !strings.Contains(err.Error(), "The value Nuke for ResetType is not valid") {
		t.Fatalf("the BMC's sentence was lost: %v", err)
	}
}

func TestAnAccountThatMayNotResetIsAnAuthError(t *testing.T) {
	bmc := &resetBMC{status: 403}
	srv := bmc.server(t)
	err := newAdapter(t).Action(context.Background(), endpointFor(t, srv),
		"Actions/ComputerSystem.Reset", map[string]any{"ResetType": "On"})
	if !errors.Is(err, models.ErrAuth) {
		t.Fatalf("want an auth error, got %v", err)
	}
}

func TestARejectedTokenIsRenewedOnceThenTheActionRuns(t *testing.T) {
	bmc := &resetBMC{reject: 1}
	srv := bmc.server(t)
	err := newAdapter(t).Action(context.Background(), endpointFor(t, srv),
		"Actions/ComputerSystem.Reset", map[string]any{"ResetType": "On"})
	if err != nil {
		t.Fatalf("a dead token should be renewed, got %v", err)
	}
	if bmc.resets != 1 || bmc.fake.sessions != 2 {
		t.Fatalf("resets=%d sessions=%d, want 1 and 2", bmc.resets, bmc.fake.sessions)
	}
}

func TestAnErrorBodyThatIsNotRedfishStillReadsAsAShortLine(t *testing.T) {
	long := strings.Repeat("x", 500)
	got := errorMessage(strings.NewReader(long))
	if len(got) != 200 {
		t.Fatalf("want 200 characters, got %d", len(got))
	}
	if errorMessage(strings.NewReader("")) != "no detail" {
		t.Fatal("an empty body should say so")
	}
}
