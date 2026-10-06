package snmp

import (
	"errors"
	"net"
	"sync"
	"sync/atomic"
	"time"

	g "github.com/gosnmp/gosnmp"
	"golang.org/x/net/ipv4"
)

// usmStatsUnknownEngineIDs is the report an authoritative engine sends a
// sender that does not know its engine ID yet (RFC 3414 3.2 step 3b), and
// usmStatsNotInTimeWindows the one it sends a sender whose notion of its
// clock is stale (3.2 step 7a).
const (
	usmStatsUnknownEngineIDs = ".1.3.6.1.6.3.15.1.1.4.0"
	usmStatsNotInTimeWindows = ".1.3.6.1.6.3.15.1.1.2.0"
)

// Reasons a v3 message is refused, as the receiver's trap counter labels.
const (
	rejectUnauthenticated = "unauthenticated"
	rejectNotInTimeWindow = "not_in_time_window"
)

// digestLen is each HMAC's truncated length on the wire (RFC 3414, 7860).
var digestLen = map[g.SnmpV3AuthProtocol]int{
	g.MD5: 12, g.SHA: 12, g.SHA224: 16, g.SHA256: 24, g.SHA384: 32, g.SHA512: 48,
}

// trapSocket is the trap receiver's UDP loop. It replaces gosnmp's
// TrapListener for one reason: a reply must leave from the address the
// request arrived on.
//
// The receiver binds the wildcard, because the address devices send to is a
// pool's trap VIP that VRRP moves between members - a backup does not own it,
// so it could not bind it. A wildcard socket's reply goes out from whatever
// source the kernel picks, and SNMP senders match a reply against the address
// they sent to: pysnmp drops an engine-ID report that comes from elsewhere
// ("Unknown SNMP engine ID encountered"), and every INFORM through a VIP
// failed. Measured live, 2026-10-05: same listener, wildcard bind - no ack;
// bound to the VIP - acked.
//
// net-snmp's snmptrapd solves this the same way: IP_PKTINFO tells it each
// datagram's destination, and the reply is sent with that as its source.
// Where the platform has no IP_PKTINFO the kernel picks, as before.
//
// Decoding is gosnmp's own (UnmarshalTrap, which re-localises the user's keys
// to each sender's engine ID); the engine-ID report and the INFORM response
// follow gosnmp's listener, which pysnmp has been verified against.
type trapSocket struct {
	Params    *g.GoSNMP
	OnNewTrap func(*g.SnmpPacket, *net.UDPAddr)
	// OnReject counts a v3 message refused for one of the reject* reasons.
	OnReject func(reason string)

	// This receiver's own engine clock, for the INFORMs it is authoritative
	// for: boots persisted across restarts, time counted from start.
	Boots uint32
	Start time.Time
	// Each sending engine's clock, for the TRAPs it is authoritative for.
	Times *EngineTimes

	listening chan bool
	mu        sync.Mutex
	conn      *net.UDPConn
	closed    bool

	unknownEngineIDs uint32
}

func newTrapSocket() *trapSocket {
	return &trapSocket{listening: make(chan bool)}
}

// Listening is closed once the socket is bound.
func (s *trapSocket) Listening() <-chan bool { return s.listening }

// Listen binds addr and reads until Close. A Close before the bind makes
// Listen return at once without binding - unlike gosnmp's listener, which
// then bound and blocked forever.
func (s *trapSocket) Listen(addr string) error {
	ua, err := net.ResolveUDPAddr("udp4", addr)
	if err != nil {
		return err
	}
	conn, err := net.ListenUDP("udp4", ua)
	if err != nil {
		return err
	}
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		_ = conn.Close()
		return nil
	}
	s.conn = conn
	s.mu.Unlock()

	pc := ipv4.NewPacketConn(conn)
	pktinfo := pc.SetControlMessage(ipv4.FlagDst, true) == nil
	close(s.listening)

	buf := make([]byte, 65535)
	for {
		n, cm, src, err := pc.ReadFrom(buf)
		if err != nil {
			if s.isClosed() || errors.Is(err, net.ErrClosed) {
				return nil
			}
			continue
		}
		remote, ok := src.(*net.UDPAddr)
		if !ok {
			continue
		}
		var dst net.IP
		if pktinfo && cm != nil {
			dst = cm.Dst
		}
		s.handle(pc, append([]byte(nil), buf[:n]...), remote, dst)
	}
}

func (s *trapSocket) Close() {
	s.mu.Lock()
	s.closed = true
	conn := s.conn
	s.mu.Unlock()
	if conn != nil {
		_ = conn.Close()
	}
}

func (s *trapSocket) isClosed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closed
}

func (s *trapSocket) handle(pc *ipv4.PacketConn, msg []byte, remote *net.UDPAddr, dst net.IP) {
	trap, err := s.Params.UnmarshalTrap(msg, false)
	if err != nil {
		return
	}
	if trap.Version == g.Version3 && trap.SecurityModel == g.UserSecurityModel &&
		s.Params.SecurityModel == g.UserSecurityModel {
		own, okOwn := s.Params.SecurityParameters.(*g.UsmSecurityParameters)
		got, okGot := trap.SecurityParameters.(*g.UsmSecurityParameters)
		if !okOwn || !okGot {
			return
		}
		if got.AuthoritativeEngineID != own.AuthoritativeEngineID {
			// RFC 3411 5: an engine ID is 5-32 octets. Anything else is a
			// sender discovering this receiver's engine before an INFORM.
			if n := len(got.AuthoritativeEngineID); n < 5 || n > 32 {
				s.reportEngineID(pc, trap, own.AuthoritativeEngineID, remote, dst)
				return
			}
		}
		if !s.secure(trap, own, got, pc, remote, dst) {
			return
		}
	}

	s.OnNewTrap(trap, remote)

	if trap.PDUType == g.InformRequest {
		// The response carries the same variables back (RFC 3416 4.2.7).
		trap.PDUType = g.GetResponse
		trap.Error = g.NoError
		trap.ErrorIndex = 0
		s.send(pc, trap, remote, dst)
	}
}

// secure is RFC 3414 3.2 steps 5-7 for a v3 message that gosnmp has decoded.
//
// gosnmp compares only as many digest bytes as the message carries, so a
// message with none - noAuthNoPriv under this user's name, or auth flags
// over an empty digest - passed as authentic, and anyone who knew the user
// name could raise alarms without a key. A message below the user's security
// level, or with a digest of the wrong length, is refused here.
//
// Then timeliness, which gosnmp never checks: an authentic message outside
// the 150 s window is a replay. For a TRAP the sender's engine is
// authoritative and its clock is tracked in Times; for an INFORM this
// receiver is, and a stale sender gets a notInTimeWindow report with this
// receiver's real boots and time so it can resynchronise and resend.
func (s *trapSocket) secure(trap *g.SnmpPacket, own, got *g.UsmSecurityParameters,
	pc *ipv4.PacketConn, remote *net.UDPAddr, dst net.IP) bool {

	need := s.Params.MsgFlags & g.AuthPriv
	if trap.MsgFlags&g.AuthPriv < need ||
		(need&g.AuthNoPriv != 0 && len(got.AuthenticationParameters) != digestLen[own.AuthenticationProtocol]) {
		s.reject(rejectUnauthenticated)
		return false
	}
	if trap.MsgFlags&g.AuthNoPriv == 0 {
		return true // nothing authentic to judge the time of
	}
	if got.AuthoritativeEngineID == own.AuthoritativeEngineID {
		now := s.engineTime()
		if got.AuthoritativeEngineBoots != s.Boots || s.Boots >= maxBoots ||
			absDiff(got.AuthoritativeEngineTime, now) > timeWindow {
			s.reject(rejectNotInTimeWindow)
			if trap.MsgFlags&g.Reportable != 0 || trap.PDUType == g.InformRequest {
				s.reportNotInTimeWindow(pc, trap, remote, dst)
			}
			return false
		}
		return true
	}
	if s.Times != nil && !s.Times.Accept(got.AuthoritativeEngineID,
		got.AuthoritativeEngineBoots, got.AuthoritativeEngineTime, time.Now()) {
		s.reject(rejectNotInTimeWindow)
		return false
	}
	return true
}

func (s *trapSocket) engineTime() uint32 {
	if s.Start.IsZero() {
		return 0
	}
	return uint32(time.Since(s.Start) / time.Second)
}

func (s *trapSocket) reject(reason string) {
	if s.OnReject != nil {
		s.OnReject(reason)
	}
}

// reportNotInTimeWindow tells an INFORM sender this receiver's real boots and
// time. Authenticated (authNoPriv), as RFC 3414 3.2 step 7a has it, so the
// sender can trust the clock it resynchronises to.
func (s *trapSocket) reportNotInTimeWindow(pc *ipv4.PacketConn, trap *g.SnmpPacket,
	remote *net.UDPAddr, dst net.IP) {
	sp, ok := trap.SecurityParameters.Copy().(*g.UsmSecurityParameters)
	if !ok {
		return
	}
	sp.AuthoritativeEngineBoots = s.Boots
	sp.AuthoritativeEngineTime = s.engineTime()
	trap.PDUType = g.Report
	trap.MsgFlags = g.AuthNoPriv
	trap.SecurityParameters = sp
	trap.Variables = []g.SnmpPDU{{Name: usmStatsNotInTimeWindows, Type: g.Integer, Value: 1}}
	s.send(pc, trap, remote, dst)
}

func (s *trapSocket) reportEngineID(pc *ipv4.PacketConn, trap *g.SnmpPacket, engineID string,
	remote *net.UDPAddr, dst net.IP) {
	sp, ok := trap.SecurityParameters.Copy().(*g.UsmSecurityParameters)
	if !ok {
		return
	}
	sp.AuthoritativeEngineID = engineID
	// This receiver's real clock, so the sender's first authenticated message
	// is already inside the window rather than refused and resent.
	sp.AuthoritativeEngineBoots = s.Boots
	sp.AuthoritativeEngineTime = s.engineTime()
	trap.PDUType = g.Report
	trap.MsgFlags &= g.AuthPriv
	trap.SecurityParameters = sp
	trap.Variables = []g.SnmpPDU{{Name: usmStatsUnknownEngineIDs, Type: g.Integer,
		Value: int(atomic.AddUint32(&s.unknownEngineIDs, 1))}}
	s.send(pc, trap, remote, dst)
}

// send replies to remote from dst, the address the request arrived on.
func (s *trapSocket) send(pc *ipv4.PacketConn, p *g.SnmpPacket, remote *net.UDPAddr, dst net.IP) {
	b, err := p.MarshalMsg()
	if err != nil {
		return
	}
	var cm *ipv4.ControlMessage
	if dst != nil && !dst.IsUnspecified() {
		cm = &ipv4.ControlMessage{Src: dst}
	}
	_, _ = pc.WriteTo(b, cm, remote)
}
