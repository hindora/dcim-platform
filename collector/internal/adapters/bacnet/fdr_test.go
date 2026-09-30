package bacnet

import (
	"context"
	"io"
	"log/slog"
	"net"
	"sync"
	"testing"
	"time"
)

// fakeBBMD is a real UDP server that speaks just enough Annex J to exercise
// RegisterForeignDevice against real bytes on real sockets, not a mock of the
// wire format.
type fakeBBMD struct {
	mu            sync.Mutex
	registrations int
	lastTTL       uint16
	resultCode    uint16 // sent back in every BVLC-Result; 0 = success
	silent        bool   // never reply, to exercise the timeout path
}

func startFakeBBMD(t *testing.T, bbmd *fakeBBMD) string {
	t.Helper()
	conn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { conn.Close() })

	go func() {
		buf := make([]byte, 32)
		for {
			n, addr, err := conn.ReadFrom(buf)
			if err != nil {
				return // socket closed at test end
			}
			if n < 6 || buf[0] != bvllType || buf[1] != bvlcFuncRegisterForeignDevice {
				continue
			}
			bbmd.mu.Lock()
			bbmd.registrations++
			bbmd.lastTTL = uint16(buf[4])<<8 | uint16(buf[5])
			silent := bbmd.silent
			code := bbmd.resultCode
			bbmd.mu.Unlock()
			if silent {
				continue
			}
			resp := []byte{bvllType, bvlcFuncResult, 0x00, 0x06,
				byte(code >> 8), byte(code)}
			_, _ = conn.WriteTo(resp, addr)
		}
	}()
	return conn.LocalAddr().String()
}

func (b *fakeBBMD) count() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.registrations
}

func TestRegisterForeignDeviceSucceedsAgainstARealBBMD(t *testing.T) {
	bbmd := &fakeBBMD{}
	addr := startFakeBBMD(t, bbmd)

	err := RegisterForeignDevice(context.Background(), addr, 60*time.Second, time.Second)
	if err != nil {
		t.Fatalf("RegisterForeignDevice: %v", err)
	}
	if bbmd.count() != 1 {
		t.Fatalf("bbmd saw %d registrations, want 1", bbmd.count())
	}
}

func TestRegisterForeignDeviceSendsTheTTLBigEndian(t *testing.T) {
	bbmd := &fakeBBMD{}
	addr := startFakeBBMD(t, bbmd)

	if err := RegisterForeignDevice(context.Background(), addr, 300*time.Second, time.Second); err != nil {
		t.Fatalf("RegisterForeignDevice: %v", err)
	}
	bbmd.mu.Lock()
	got := bbmd.lastTTL
	bbmd.mu.Unlock()
	if got != 300 {
		t.Errorf("bbmd read ttl=%d, want 300", got)
	}
}

func TestRegisterForeignDeviceFailsOnANonZeroResultCode(t *testing.T) {
	bbmd := &fakeBBMD{resultCode: 0x0010} // "register foreign device" NAK code
	addr := startFakeBBMD(t, bbmd)

	err := RegisterForeignDevice(context.Background(), addr, 60*time.Second, time.Second)
	if err == nil {
		t.Fatal("expected an error for a non-zero BVLC-Result code")
	}
}

func TestRegisterForeignDeviceTimesOutWhenTheBBMDIsSilent(t *testing.T) {
	bbmd := &fakeBBMD{silent: true}
	addr := startFakeBBMD(t, bbmd)

	err := RegisterForeignDevice(context.Background(), addr, 60*time.Second, 200*time.Millisecond)
	if err == nil {
		t.Fatal("expected a timeout error against a silent BBMD")
	}
}

func TestRegisterForeignDeviceFailsAgainstAClosedPort(t *testing.T) {
	conn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := conn.LocalAddr().String()
	conn.Close() // nothing is listening there now

	err = RegisterForeignDevice(context.Background(), addr, 60*time.Second, 200*time.Millisecond)
	if err == nil {
		t.Fatal("expected an error against an address nothing listens on")
	}
}

func TestRenewLoopRegistersImmediatelyThenRepeatedly(t *testing.T) {
	bbmd := &fakeBBMD{}
	addr := startFakeBBMD(t, bbmd)
	log := slog.New(slog.NewTextHandler(io.Discard, nil))

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	// ttl=100ms -> renew interval 50ms: immediate registration plus several
	// renewals inside one second. Generous margins - this only needs to prove
	// "repeatedly", not pin an exact count against timer jitter.
	RenewForeignDeviceRegistration(ctx, addr, 100*time.Millisecond, 200*time.Millisecond, log)

	if got := bbmd.count(); got < 3 {
		t.Fatalf("bbmd saw %d registrations in 1s at a 50ms renew interval, want >= 3", got)
	}
}

func TestRenewLoopStopsWhenContextIsCancelled(t *testing.T) {
	bbmd := &fakeBBMD{}
	addr := startFakeBBMD(t, bbmd)
	log := slog.New(slog.NewTextHandler(io.Discard, nil))

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		RenewForeignDeviceRegistration(ctx, addr, 50*time.Millisecond, 50*time.Millisecond, log)
		close(done)
	}()

	time.Sleep(20 * time.Millisecond) // let the immediate registration land
	cancel()

	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("RenewForeignDeviceRegistration did not return after context cancellation")
	}
}
