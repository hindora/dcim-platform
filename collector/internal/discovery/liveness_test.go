package discovery

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"strconv"
	"syscall"
	"testing"
	"time"
)

// fakeDial: a host listed in `up` refuses, as a live host's kernel does;
// anything else stays silent until the check gives up.
func fakeDial(up ...string) func(ctx context.Context, hostport string) error {
	live := map[string]bool{}
	for _, a := range up {
		live[a] = true
	}
	return func(ctx context.Context, hostport string) error {
		host, _, _ := net.SplitHostPort(hostport)
		if live[host] {
			return &net.OpError{Op: "dial", Err: syscall.ECONNREFUSED}
		}
		<-ctx.Done()
		return ctx.Err()
	}
}

func TestLivenessKeepsWhatAnswersAndWhatIsExpected(t *testing.T) {
	// The empty address is skipped; the expected one is kept although it
	// answers nothing - an ACL'd device the sweep must still ask, or it reads
	// as missing. Order is kept: the sweep's results follow the scope.
	l := &Liveness{TCPPorts: []int{22, 80}, Timeout: 100 * time.Millisecond,
		dial: fakeDial("10.0.0.2")}
	got := l.Filter(context.Background(),
		[]string{"10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"},
		map[string]bool{"10.0.0.4": true})
	if want := []string{"10.0.0.2", "10.0.0.4"}; !equal(got, want) {
		t.Errorf("kept %v, want %v", got, want)
	}
}

func TestLivenessCostsOneShortWaitPerEmptyAddress(t *testing.T) {
	// 256 empty addresses, 64 at a time, 100 ms each: ~0.4 s. The SNMP probes
	// this replaces cost ~24 s per empty address.
	addrs := make([]string, 256)
	for i := range addrs {
		addrs[i] = "10.9.0." + strconv.Itoa(i)
	}
	l := &Liveness{TCPPorts: DefaultLivenessPorts, Timeout: 100 * time.Millisecond,
		Concurrency: 64, dial: fakeDial()}
	start := time.Now()
	if got := l.Filter(context.Background(), addrs, nil); len(got) != 0 {
		t.Fatalf("kept %v from an empty network", got)
	}
	if took := time.Since(start); took > 2*time.Second {
		t.Errorf("256 empty addresses took %v", took)
	}
}

func TestARealRefusalCountsAsAnswer(t *testing.T) {
	// The kernel's RST on a closed port, for real: a port just closed.
	ln, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	_ = ln.Close()
	l := NewLiveness(slog.Default(), []int{port}, time.Second, 1)
	if !l.Alive(context.Background(), "127.0.0.1") {
		t.Error("a refused connect was not counted as someone there")
	}
}

func TestRefusedIsOnlyARefusal(t *testing.T) {
	if refused(errors.New("i/o timeout")) || refused(context.DeadlineExceeded) {
		t.Error("silence counted as a refusal")
	}
	if !refused(&net.OpError{Err: syscall.ECONNREFUSED}) || !refused(&net.OpError{Err: syscall.Errno(10061)}) {
		t.Error("a refusal was not recognised")
	}
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
