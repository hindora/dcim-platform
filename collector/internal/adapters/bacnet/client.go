package bacnet

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"sync"
	"time"
)

// Client is the BACnet/IP transport: ONE UDP socket for every device.
//
// A socket per endpoint would be the obvious design and the wrong one. BACnet
// is a broadcast protocol - Who-Is, I-Am and unconfirmed COV notifications all
// arrive unsolicited, addressed to the port rather than to a conversation - so
// a per-device socket receives none of them, and at a few hundred devices it
// also burns file descriptors the collector needs elsewhere.
//
// Requests are correlated by invoke ID, drawn from a pool of the 256 values
// the protocol allows. The pool is not an optimisation: reusing an ID that is
// still in flight makes a late reply for the previous request look like the
// answer to the current one, and the value that lands is real, plausible, and
// attributed to the wrong point.
type Client struct {
	conn *net.UDPConn
	log  *slog.Logger

	port    int
	timeout time.Duration
	retries int

	mu      sync.Mutex
	pending map[byte]*call
	free    []byte

	// Unsolicited traffic (I-Am, COV notifications) goes here.
	OnUnsolicited func(src Address, a apdu)

	closed chan struct{}
	wg     sync.WaitGroup
}

type call struct {
	expect Address
	ch     chan result
}

type result struct {
	apdu apdu
	src  Address
	err  error
}

// ErrNoInvokeID means every invoke ID is in flight. It is a back-pressure
// signal, not a device fault: the caller should shed rather than queue, since
// a queued BACnet request is usually stale by the time it is sent.
var ErrNoInvokeID = errors.New("bacnet: all invoke ids in flight")

// ErrTimeout is no reply within the APDU timeout after all retries.
var ErrTimeout = errors.New("bacnet: timeout")

func NewClient(port int, timeout time.Duration, retries int, log *slog.Logger) *Client {
	if timeout <= 0 {
		timeout = 3 * time.Second // the standard's default APDU timeout
	}
	if retries < 0 {
		retries = 0
	}
	c := &Client{
		port: port, timeout: timeout, retries: retries, log: log,
		pending: make(map[byte]*call), closed: make(chan struct{}),
	}
	c.free = make([]byte, 0, 256)
	for i := 0; i < 256; i++ {
		c.free = append(c.free, byte(i))
	}
	return c
}

// Open binds the socket and starts the receive loop.
//
// Port 0 asks the kernel for an ephemeral port, which is the right default
// here: replies come back to the source address of the request, and 47808 is
// usually already held by whatever else on the host speaks BACnet. Binding
// 47808 is only necessary to receive broadcasts, which matters for discovery
// but not for polling.
func (c *Client) Open() error {
	addr := &net.UDPAddr{IP: net.IPv4zero, Port: c.port}
	conn, err := net.ListenUDP("udp4", addr)
	if err != nil {
		return fmt.Errorf("bind bacnet socket on port %d: %w", c.port, err)
	}
	c.conn = conn
	c.wg.Add(1)
	go c.receive()
	c.log.Info("bacnet socket open", "addr", conn.LocalAddr().String())
	return nil
}

func (c *Client) Close() error {
	select {
	case <-c.closed:
		return nil
	default:
		close(c.closed)
	}
	if c.conn != nil {
		_ = c.conn.Close()
	}
	c.wg.Wait()
	return nil
}

func (c *Client) receive() {
	defer c.wg.Done()
	// A whole object list arrives in one datagram, so the buffer has to be
	// bigger than a max-APDU: a short read would truncate the list silently.
	buf := make([]byte, 65535)
	for {
		n, from, err := c.conn.ReadFromUDP(buf)
		if err != nil {
			select {
			case <-c.closed:
				return
			default:
			}
			c.log.Warn("bacnet read failed", "error", err)
			continue
		}
		data := make([]byte, n)
		copy(data, buf[:n])
		c.dispatch(data, from)
	}
}

func (c *Client) dispatch(data []byte, from *net.UDPAddr) {
	info, err := unframe(data)
	if err != nil {
		// Not worth an error: a shared segment carries plenty of BACnet
		// traffic that is not ours.
		c.log.Debug("bacnet frame ignored", "src", from.IP.String(), "error", err)
		return
	}
	a, err := parseAPDU(info.APDU)
	if err != nil {
		c.log.Debug("bacnet apdu ignored", "src", from.IP.String(), "error", err)
		return
	}
	src := Address{IP: from.IP.String(), Net: info.SrcNet, MAC: info.SrcMAC}

	if a.Kind == kindUnconfirmed {
		if c.OnUnsolicited != nil {
			c.OnUnsolicited(src, a)
		}
		return
	}

	c.mu.Lock()
	call, ok := c.pending[a.InvokeID]
	c.mu.Unlock()
	if !ok {
		// A reply that arrived after its request gave up. Dropping it is
		// correct; delivering it to whoever holds the ID now is not.
		c.log.Debug("bacnet reply with no caller", "src", src.IP,
			"invoke_id", a.InvokeID)
		return
	}
	if !sameDevice(call.expect, src) {
		c.log.Warn("bacnet reply from an unexpected device",
			"invoke_id", a.InvokeID, "want", call.expect.IP, "got", src.IP)
		return
	}
	select {
	case call.ch <- result{apdu: a, src: src}:
	default:
	}
}

// sameDevice compares the addresses that identify a responder. For a routed
// device the IP is the router's, so the network and MAC are what distinguish
// it from its neighbours on the trunk.
func sameDevice(want, got Address) bool {
	if want.IP != got.IP {
		return false
	}
	if !want.Routed() {
		return true
	}
	if want.Net != got.Net || len(want.MAC) != len(got.MAC) {
		return false
	}
	for i := range want.MAC {
		if want.MAC[i] != got.MAC[i] {
			return false
		}
	}
	return true
}

func (c *Client) takeInvokeID() (byte, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.free) == 0 {
		return 0, ErrNoInvokeID
	}
	id := c.free[len(c.free)-1]
	c.free = c.free[:len(c.free)-1]
	return id, nil
}

func (c *Client) releaseInvokeID(id byte) {
	c.mu.Lock()
	delete(c.pending, id)
	c.free = append(c.free, id)
	c.mu.Unlock()
}

// send transmits one confirmed request and waits for its reply, retrying on
// timeout only.
//
// Retries are for timeouts alone. A device that answered with an error
// answered: repeating the request produces the same error and, for a
// controller with a small APDU queue, costs a slot that a working point needs.
func (c *Client) send(ctx context.Context, dest Address,
	build func(invokeID byte) []byte) (apdu, error) {

	id, err := c.takeInvokeID()
	if err != nil {
		return apdu{}, err
	}
	defer c.releaseInvokeID(id)

	ch := make(chan result, 1)
	c.mu.Lock()
	c.pending[id] = &call{expect: dest, ch: ch}
	c.mu.Unlock()

	pkt := frame(build(id), dest, true, false)
	udp := &net.UDPAddr{IP: net.ParseIP(dest.IP), Port: dest.UDPPort()}
	if udp.IP == nil {
		return apdu{}, fmt.Errorf("bacnet: bad address %q", dest.IP)
	}

	for attempt := 0; attempt <= c.retries; attempt++ {
		if _, err := c.conn.WriteToUDP(pkt, udp); err != nil {
			return apdu{}, fmt.Errorf("bacnet write to %s: %w", dest.IP, err)
		}
		timer := time.NewTimer(c.timeout)
		select {
		case r := <-ch:
			timer.Stop()
			if r.err != nil {
				return apdu{}, r.err
			}
			switch r.apdu.Kind {
			case kindError, kindReject, kindAbort:
				return r.apdu, errorFrom(r.apdu)
			default:
				return r.apdu, nil
			}
		case <-timer.C:
			// Next attempt reuses the same invoke ID, which is what the
			// standard requires: a retry is the same request, not a new one.
		case <-ctx.Done():
			timer.Stop()
			return apdu{}, ctx.Err()
		case <-c.closed:
			timer.Stop()
			return apdu{}, net.ErrClosed
		}
	}
	return apdu{}, fmt.Errorf("%w: %s after %d attempts", ErrTimeout, dest.IP,
		c.retries+1)
}

// ReadProperty reads one property.
func (c *Client) ReadProperty(ctx context.Context, dest Address, obj ObjectID,
	prop uint32) ([]Value, error) {

	a, err := c.send(ctx, dest, func(id byte) []byte {
		return readPropertyRequest(id, obj, prop)
	})
	if err != nil {
		return nil, err
	}
	_, _, vals, err := parseReadPropertyAck(a.Payload)
	return vals, err
}

// ReadPropertyIndex reads one element of an array property. Element 0 of
// object-list is its length.
func (c *Client) ReadPropertyIndex(ctx context.Context, dest Address, obj ObjectID,
	prop, index uint32) ([]Value, error) {

	a, err := c.send(ctx, dest, func(id byte) []byte {
		return readPropertyIndexRequest(id, obj, prop, index)
	})
	if err != nil {
		return nil, err
	}
	_, _, vals, err := parseReadPropertyAck(a.Payload)
	return vals, err
}

// ReadPropertyMultiple reads several objects in one exchange.
func (c *Client) ReadPropertyMultiple(ctx context.Context, dest Address,
	specs []ReadSpec) ([]RPMResult, error) {

	a, err := c.send(ctx, dest, func(id byte) []byte {
		return readPropertyMultipleRequest(id, specs)
	})
	if err != nil {
		return nil, err
	}
	return parseRPMAck(a.Payload)
}

// WhoIs broadcasts a discovery request. Replies arrive asynchronously as I-Am
// through OnUnsolicited, because a broadcast has no single responder to wait
// for - which is exactly why discovery cannot be modelled as a request.
func (c *Client) WhoIs(broadcastIP string, port int, low, high uint32, ranged bool) error {
	dest := Address{IP: broadcastIP, Port: port}
	udp := &net.UDPAddr{IP: net.ParseIP(broadcastIP), Port: dest.UDPPort()}
	if udp.IP == nil {
		return fmt.Errorf("bacnet: bad broadcast address %q", broadcastIP)
	}
	pkt := frame(whoIsRequest(low, high, ranged), dest, false, true)
	_, err := c.conn.WriteToUDP(pkt, udp)
	return err
}

// SubscribeCOV asks a device to push changes for one object.
//
// The lifetime is deliberately finite. A subscription with an infinite
// lifetime survives the collector that created it, and a controller with a
// small subscription table fills up with ghosts until new subscriptions fail
// silently - the same failure mode as orphaned Redfish event destinations.
func (c *Client) SubscribeCOV(ctx context.Context, dest Address, processID uint32,
	obj ObjectID, confirmed bool, lifetime time.Duration) error {

	secs := uint32(lifetime.Seconds())
	_, err := c.send(ctx, dest, func(id byte) []byte {
		return subscribeCOVRequest(id, processID, obj, confirmed, secs)
	})
	return err
}

// InFlight reports how many invoke IDs are currently held, for the health
// gauge. A number pinned near 256 means the collector is the bottleneck, not
// the plane.
func (c *Client) InFlight() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return 256 - len(c.free)
}

// LocalAddr is the bound address, useful in logs when the port was ephemeral.
func (c *Client) LocalAddr() string {
	if c.conn == nil {
		return ""
	}
	return c.conn.LocalAddr().String()
}
