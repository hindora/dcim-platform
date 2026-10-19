package snmp

import (
	"context"
	"fmt"
	"strings"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// SetInteger writes one INTEGER value to one OID on the endpoint's agent.
//
// The collector is otherwise read-only; this exists for a device action the
// platform has queued (switching a PDU outlet). It dials the way a poll does -
// same credential, same timeout, same v3 handling - so a write is made with the
// authority the endpoint was given and no other. `port` overrides the polled
// port when the agent takes writes elsewhere; zero means the polled port, which
// is what real gear does.
//
// A refusal is reported as the device said it, in words: noSuchName on an
// outlet-control column means the strip has no relay, which is a fact about the
// hardware and not a fault of the request.
func (a *Adapter) SetInteger(ctx context.Context, ep *models.Endpoint, port int,
	oid string, value int) error {
	target := *ep
	if port > 0 {
		target.Port = port
	}
	client, _, err := a.dial(ctx, &target, 1, false)
	if err != nil {
		return err
	}
	defer client.Conn.Close()

	pkt, err := client.Set([]g.SnmpPDU{{
		Name:  "." + strings.TrimPrefix(oid, "."),
		Type:  g.Integer,
		Value: value,
	}})
	if err != nil {
		return fmt.Errorf("%w: %v", models.ErrUnreachable, err)
	}
	if pkt.Error != g.NoError {
		return setError(pkt.Error)
	}
	return nil
}

// setError turns an SNMP error-status into what an operator can act on.
func setError(s g.SNMPError) error {
	switch s {
	case g.NoSuchName:
		return fmt.Errorf("%w: the device has no such control (a metered strip has no relay, "+
			"or it uses a different MIB)", models.ErrProtocolStatus)
	case g.BadValue, g.WrongValue, g.WrongType, g.WrongLength, g.WrongEncoding:
		return fmt.Errorf("%w: the device refused the value (%s)", models.ErrProtocolStatus, s)
	case g.ReadOnly, g.NoAccess, g.NotWritable, g.AuthorizationError:
		return fmt.Errorf("%w: the device refused the write (%s): the credential is read-only",
			models.ErrAuth, s)
	default:
		return fmt.Errorf("%w: SNMP error %s", models.ErrProtocolStatus, s)
	}
}
