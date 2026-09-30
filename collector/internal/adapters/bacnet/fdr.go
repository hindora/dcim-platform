package bacnet

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"time"
)

// Foreign Device Registration (BACnet/IP Annex J.5.2) is how a device off the
// BBMD's own subnet - the far side of a WAN link, or simply not on the same
// broadcast domain - gets included in BACnet/IP's broadcast distribution: it
// asks a BBMD to relay broadcasts to it as unicasts for a bounded time, then
// keeps re-asking.
//
// docs/26 Phase 9 makes this opt-in per the "static unicast by default" call:
// a directed Who-Is (what Adapter.discover already does when an endpoint's
// device_instance is unknown) needs no broadcast and no facilities change
// beyond one firewall pinhole. FDR is for the one case that still needs a
// broadcast domain - device discovery on a subnet this collector is not on -
// which is why it is a distinct, explicitly configured mode rather than
// something every BACnet collector does by default.
const (
	bvlcFuncResult                = 0x00
	bvlcFuncRegisterForeignDevice = 0x05
	bvlcResultSuccess             = 0x0000
)

// RegisterForeignDevice sends one Register-Foreign-Device request to a BBMD
// and waits for its BVLC-Result acknowledgement. It does not renew - see
// RenewForeignDeviceRegistration for the TTL-driven loop a running collector
// needs, since a BBMD forgets the entry once the TTL it was given lapses.
func RegisterForeignDevice(ctx context.Context, bbmdAddr string, ttl, timeout time.Duration) error {
	if ttl <= 0 {
		ttl = 60 * time.Second
	}
	if timeout <= 0 {
		timeout = 3 * time.Second
	}

	conn, err := net.Dial("udp", bbmdAddr)
	if err != nil {
		return fmt.Errorf("register-foreign-device: could not reach %s: %w", bbmdAddr, err)
	}
	defer conn.Close()

	deadline := time.Now().Add(timeout)
	if dl, ok := ctx.Deadline(); ok && dl.Before(deadline) {
		deadline = dl
	}
	if err := conn.SetDeadline(deadline); err != nil {
		return err
	}

	secs := uint16(ttl / time.Second)
	req := []byte{
		bvllType, bvlcFuncRegisterForeignDevice,
		0x00, 0x06, // BVLC length: 4-byte header + 2-byte TTL
		byte(secs >> 8), byte(secs),
	}
	if _, err := conn.Write(req); err != nil {
		return fmt.Errorf("register-foreign-device: could not send to %s: %w", bbmdAddr, err)
	}

	resp := make([]byte, 32)
	n, err := conn.Read(resp)
	if err != nil {
		return fmt.Errorf("register-foreign-device: no reply from %s: %w", bbmdAddr, err)
	}
	if n < 6 || resp[0] != bvllType {
		return fmt.Errorf("register-foreign-device: %s sent a malformed reply", bbmdAddr)
	}
	if resp[1] != bvlcFuncResult {
		return fmt.Errorf(
			"register-foreign-device: %s replied with BVLC function 0x%02X, not a BVLC-Result",
			bbmdAddr, resp[1])
	}
	code := uint16(resp[4])<<8 | uint16(resp[5])
	if code != bvlcResultSuccess {
		return fmt.Errorf(
			"register-foreign-device: %s refused registration, result code 0x%04X",
			bbmdAddr, code)
	}
	return nil
}

// RenewForeignDeviceRegistration keeps this collector's entry in the BBMD's
// foreign device table alive for as long as ctx runs, registering
// immediately and then again every half of ttl.
//
// Half rather than "just before it expires": unlike a certificate renewal
// there is no grace window on the BBMD's side (Annex J.5.2.3) - the entry is
// simply gone once the TTL lapses - so renewing at the midpoint leaves a full
// TTL of slack against one missed attempt (a busy BBMD, a transient WAN
// blip) before this collector actually drops out of the broadcast domain.
func RenewForeignDeviceRegistration(ctx context.Context, bbmdAddr string,
	ttl, timeout time.Duration, log *slog.Logger) {

	if ttl <= 0 {
		ttl = 60 * time.Second
	}
	interval := ttl / 2

	register := func() {
		if err := RegisterForeignDevice(ctx, bbmdAddr, ttl, timeout); err != nil {
			log.Warn("foreign device registration failed", "bbmd", bbmdAddr, "error", err)
			return
		}
		log.Info("registered with BBMD", "bbmd", bbmdAddr, "ttl", ttl)
	}

	register()
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			register()
		}
	}
}
