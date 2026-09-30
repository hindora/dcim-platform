package preflight

import (
	"fmt"
	"net"
)

// trapPortBindable opens and immediately closes a UDP listener on addr -
// exactly what the real trap receiver (internal/adapters/snmp.TrapReceiver)
// will try to do when the collector actually starts. Checked here instead
// of just trusting the config, because "the port is bindable" is a
// question about the HOST (something else already listening, a
// permissions problem on a privileged port), not about anything this
// collector's own configuration can get wrong.
func trapPortBindable(addr string) error {
	conn, err := net.ListenPacket("udp", addr)
	if err != nil {
		return fmt.Errorf("cannot bind %s: %w", addr, err)
	}
	return conn.Close()
}
