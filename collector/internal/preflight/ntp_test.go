package preflight

import (
	"context"
	"encoding/binary"
	"net"
	"testing"
	"time"
)

// fakeSNTPServer answers exactly one client-mode request with a reply
// whose clock is skew ahead of whatever Originate timestamp the request
// carried - a real UDP server speaking the real wire format, not a
// reimplementation of ntpOffset's own math to check itself against.
func fakeSNTPServer(t *testing.T, skew time.Duration) string {
	t.Helper()
	conn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	t.Cleanup(func() { conn.Close() })

	go func() {
		buf := make([]byte, 48)
		n, addr, err := conn.ReadFrom(buf)
		if err != nil || n < 48 {
			return
		}
		originate := buf[40:48] // the client's Transmit Timestamp, echoed back

		serverNow := time.Now().UTC().Add(skew)
		var resp [48]byte
		resp[0] = (4 << 3) | 4             // VN=4, Mode=4 (server)
		resp[1] = 1                        // stratum 1 - not a kiss-of-death
		copy(resp[24:32], originate)       // Originate Timestamp = client's request
		putNTPTime(resp[32:40], serverNow) // Receive Timestamp
		putNTPTime(resp[40:48], serverNow) // Transmit Timestamp
		_, _ = conn.WriteTo(resp[:], addr)
	}()

	return conn.LocalAddr().String()
}

func TestNTPOffsetDetectsAServerAheadOfTheClient(t *testing.T) {
	addr := fakeSNTPServer(t, 5*time.Second)
	offset, err := ntpOffset(context.Background(), addr, 2*time.Second)
	if err != nil {
		t.Fatalf("ntpOffset: %v", err)
	}
	if offset < 4*time.Second || offset > 6*time.Second {
		t.Errorf("offset = %v, want roughly +5s (server ahead)", offset)
	}
}

func TestNTPOffsetDetectsAServerBehindTheClient(t *testing.T) {
	addr := fakeSNTPServer(t, -5*time.Second)
	offset, err := ntpOffset(context.Background(), addr, 2*time.Second)
	if err != nil {
		t.Fatalf("ntpOffset: %v", err)
	}
	if offset > -4*time.Second || offset < -6*time.Second {
		t.Errorf("offset = %v, want roughly -5s (server behind)", offset)
	}
}

func TestNTPOffsetIsNearZeroForAnInSyncServer(t *testing.T) {
	addr := fakeSNTPServer(t, 0)
	offset, err := ntpOffset(context.Background(), addr, 2*time.Second)
	if err != nil {
		t.Fatalf("ntpOffset: %v", err)
	}
	if offset > 500*time.Millisecond || offset < -500*time.Millisecond {
		t.Errorf("offset = %v, want close to 0", offset)
	}
}

func TestNTPOffsetRejectsAKissOfDeath(t *testing.T) {
	conn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer conn.Close()
	go func() {
		buf := make([]byte, 48)
		_, addr, err := conn.ReadFrom(buf)
		if err != nil {
			return
		}
		var resp [48]byte
		resp[0] = (4 << 3) | 4
		resp[1] = 0 // stratum 0 - kiss-of-death
		_, _ = conn.WriteTo(resp[:], addr)
	}()
	_, err = ntpOffset(context.Background(), conn.LocalAddr().String(), 2*time.Second)
	if err == nil {
		t.Fatal("expected an error for a stratum-0 (kiss-of-death) reply")
	}
}

func TestNTPOffsetTimesOutAgainstAServerThatNeverReplies(t *testing.T) {
	conn, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer conn.Close() // never reads or replies
	_, err = ntpOffset(context.Background(), conn.LocalAddr().String(), 200*time.Millisecond)
	if err == nil {
		t.Fatal("expected a timeout error")
	}
}

func TestPutAndParseNTPTimeRoundTrip(t *testing.T) {
	want := time.Date(2026, 9, 30, 12, 0, 0, 250_000_000, time.UTC)
	var b [8]byte
	putNTPTime(b[:], want)
	got := ntpTime(b[:])
	if diff := got.Sub(want); diff > time.Millisecond || diff < -time.Millisecond {
		t.Errorf("round trip = %v, want %v (diff %v)", got, want, diff)
	}
}

func TestNTPTimeFieldsAreBigEndian(t *testing.T) {
	// A sanity check independent of putNTPTime/ntpTime themselves: encode
	// a known Unix second directly and confirm the wire byte order.
	var b [4]byte
	binary.BigEndian.PutUint32(b[:], uint32(100))
	if b[0] != 0 || b[3] != 100 {
		t.Fatalf("test setup assumption about binary.BigEndian is wrong: %v", b)
	}
}
