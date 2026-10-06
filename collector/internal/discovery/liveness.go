package discovery

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"strconv"
	"sync"
	"syscall"
	"time"

	"golang.org/x/net/icmp"
	"golang.org/x/net/ipv4"
)

// Liveness asks, cheaply, whether anything is at an address before the sweep
// spends SNMP timeouts on it.
//
// Why: the sweep tries SNMPv3 before v2c, and an address with nothing on it
// costs both - two 6 s timeouts with a retry each, ~24 s. A /20 is mostly
// empty, so a sweep of one took ~3.4 h. Asking "is anything there" first is
// how discovery tools have always done it (nmap's host discovery, LibreNMS's
// fping before snmp-scan, SolarWinds' ICMP-then-SNMP).
//
// How: a TCP connect to a few management ports, where a refusal counts - a
// live host's kernel answers RST, an empty address answers nothing - plus an
// ICMP echo when this process may open an ICMP socket (CAP_NET_RAW, or
// net.ipv4.ping_group_range covering its group). The collector's systemd unit
// grants neither and is not widened for this; TCP alone needs no privilege.
//
// What it cannot see: a network ACL'd to UDP/161 drops both. That is why the
// API sends the addresses something already says are there - inventory, and
// every earlier candidate - and those get the full probe regardless. What can
// still be missed is a NEW device on such a network; turn liveness off for a
// collector that sweeps one.
type Liveness struct {
	TCPPorts    []int
	Timeout     time.Duration
	Concurrency int
	Log         *slog.Logger

	// icmpNet is "udp4" (unprivileged ping sockets), "ip4:icmp" (raw) or ""
	// when neither can be opened.
	icmpNet string
	// dial is replaced in tests.
	dial func(ctx context.Context, addr string) error
}

const (
	defaultLivenessTimeout     = 1500 * time.Millisecond
	defaultLivenessConcurrency = 64
)

// DefaultLivenessPorts are what management interfaces listen on: SSH, and the
// web UI every PDU, UPS and BMC card ships. The ports matter less than they
// seem - a closed port answers too - but a host firewall that drops rather than
// rejects is likelier to have left one of these open.
var DefaultLivenessPorts = []int{22, 80, 443}

// NewLiveness builds a checker and finds out once whether ICMP is available.
func NewLiveness(log *slog.Logger, ports []int, timeout time.Duration, concurrency int) *Liveness {
	if len(ports) == 0 {
		ports = DefaultLivenessPorts
	}
	l := &Liveness{TCPPorts: ports, Timeout: timeout, Concurrency: concurrency, Log: log}
	for _, network := range []string{"udp4", "ip4:icmp"} {
		if c, err := icmp.ListenPacket(network, "0.0.0.0"); err == nil {
			_ = c.Close()
			l.icmpNet = network
			break
		}
	}
	log.Info("discovery liveness check", "tcp_ports", ports,
		"icmp", map[string]string{"": "unavailable", "udp4": "ping socket",
			"ip4:icmp": "raw socket"}[l.icmpNet])
	return l
}

// Filter returns the addresses worth a full probe, in their original order:
// every expected one, and every other that answered the check.
func (l *Liveness) Filter(ctx context.Context, addrs []string, expected map[string]bool) []string {
	conc := l.Concurrency
	if conc <= 0 {
		conc = defaultLivenessConcurrency
	}
	keep := make([]bool, len(addrs))
	sem := make(chan struct{}, conc)
	var wg sync.WaitGroup
	for i, addr := range addrs {
		if expected[addr] {
			keep[i] = true
			continue
		}
		if ctx.Err() != nil {
			break
		}
		wg.Add(1)
		sem <- struct{}{}
		go func(i int, addr string) {
			defer wg.Done()
			defer func() { <-sem }()
			keep[i] = l.Alive(ctx, addr)
		}(i, addr)
	}
	wg.Wait()
	out := make([]string, 0, len(addrs)/8)
	for i, addr := range addrs {
		if keep[i] {
			out = append(out, addr)
		}
	}
	return out
}

// Alive is true as soon as any check gets an answer.
func (l *Liveness) Alive(ctx context.Context, addr string) bool {
	timeout := l.Timeout
	if timeout <= 0 {
		timeout = defaultLivenessTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	checks := len(l.TCPPorts)
	if l.icmpNet != "" {
		checks++
	}
	answers := make(chan bool, checks)
	dial := l.dial
	if dial == nil {
		dial = dialTCP
	}
	for _, port := range l.TCPPorts {
		go func(port int) {
			err := dial(ctx, net.JoinHostPort(addr, strconv.Itoa(port)))
			answers <- err == nil || refused(err)
		}(port)
	}
	if l.icmpNet != "" {
		go func() { answers <- l.echo(ctx, addr) }()
	}
	for i := 0; i < checks; i++ {
		if <-answers {
			return true
		}
	}
	return false
}

// refused is a connect the host itself turned down: someone is there.
// WSAECONNREFUSED (10061) is the same answer on Windows.
func refused(err error) bool {
	var errno syscall.Errno
	return errors.As(err, &errno) && (errno == syscall.ECONNREFUSED || errno == 10061)
}

func dialTCP(ctx context.Context, hostport string) error {
	var d net.Dialer
	c, err := d.DialContext(ctx, "tcp", hostport)
	if err == nil {
		_ = c.Close()
	}
	return err
}

// echo sends one ICMP echo and waits for the reply from that address.
func (l *Liveness) echo(ctx context.Context, addr string) bool {
	ip := net.ParseIP(addr).To4()
	if ip == nil {
		return false
	}
	c, err := icmp.ListenPacket(l.icmpNet, "0.0.0.0")
	if err != nil {
		return false
	}
	defer c.Close()
	if dl, ok := ctx.Deadline(); ok {
		_ = c.SetDeadline(dl)
	}
	var dst net.Addr = &net.IPAddr{IP: ip}
	if l.icmpNet == "udp4" {
		dst = &net.UDPAddr{IP: ip}
	}
	// On a ping socket the kernel replaces the ID with the socket's own and
	// delivers only that socket's replies; on a raw one every reply arrives,
	// so the source address decides.
	msg := icmp.Message{Type: ipv4.ICMPTypeEcho, Body: &icmp.Echo{
		ID: int(time.Now().UnixNano() & 0xffff), Seq: 1, Data: []byte("dcim-discovery")}}
	b, err := msg.Marshal(nil)
	if err != nil {
		return false
	}
	if _, err := c.WriteTo(b, dst); err != nil {
		return false
	}
	buf := make([]byte, 1500)
	for {
		n, peer, err := c.ReadFrom(buf)
		if err != nil {
			return false
		}
		rm, err := icmp.ParseMessage(1, buf[:n])
		if err != nil || rm.Type != ipv4.ICMPTypeEchoReply {
			continue
		}
		var from net.IP
		switch p := peer.(type) {
		case *net.UDPAddr:
			from = p.IP
		case *net.IPAddr:
			from = p.IP
		}
		if from.Equal(ip) {
			return true
		}
	}
}
