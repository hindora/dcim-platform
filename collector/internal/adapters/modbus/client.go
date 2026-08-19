package modbus

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"sync"
	"time"
)

// Client holds one TCP connection per device address and serialises the
// requests that share it.
//
// Connections are kept open, not opened per poll. A Modbus/TCP handshake is
// cheap but the devices are not: real gear accepts a handful of concurrent
// masters (Vertiv and Schneider cards typically cap at four to eight) and
// refuses the rest, so a collector that reconnects every cycle spends its
// budget on sockets a meter has to account for.
//
// Requests to one address are serialised by a mutex rather than pipelined.
// Modbus/TCP permits several outstanding transactions, but a serial gateway
// behind the socket does not: it forwards one RS-485 transaction at a time,
// and pipelining into it only queues inside the gateway where the collector
// cannot see the delay.
type Client struct {
	log     *slog.Logger
	timeout time.Duration
	// Retries apply to TRANSPORT failures only. A device that answered with an
	// exception answered; repeating the request produces the same exception
	// and costs a slot the working points need.
	retries int

	mu    sync.Mutex
	conns map[string]*conn
}

type conn struct {
	mu   sync.Mutex // serialises requests on this socket
	sock net.Conn
	txn  uint16
	addr string
}

func NewClient(timeout time.Duration, retries int, log *slog.Logger) *Client {
	if timeout <= 0 {
		timeout = 3 * time.Second
	}
	if retries < 0 {
		retries = 0
	}
	return &Client{
		log: log, timeout: timeout, retries: retries,
		conns: make(map[string]*conn),
	}
}

// Close drops every connection.
func (c *Client) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, cn := range c.conns {
		cn.mu.Lock()
		if cn.sock != nil {
			_ = cn.sock.Close()
			cn.sock = nil
		}
		cn.mu.Unlock()
	}
	c.conns = make(map[string]*conn)
	return nil
}

// Forget drops the connection to one address, so the next request reconnects.
func (c *Client) Forget(addr string) {
	c.mu.Lock()
	cn, ok := c.conns[addr]
	delete(c.conns, addr)
	c.mu.Unlock()
	if ok {
		cn.mu.Lock()
		if cn.sock != nil {
			_ = cn.sock.Close()
			cn.sock = nil
		}
		cn.mu.Unlock()
	}
}

// Connections reports how many sockets are held, for the health gauge.
func (c *Client) Connections() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.conns)
}

func (c *Client) connFor(addr string) *conn {
	c.mu.Lock()
	defer c.mu.Unlock()
	if cn, ok := c.conns[addr]; ok {
		return cn
	}
	cn := &conn{addr: addr}
	c.conns[addr] = cn
	return cn
}

// request sends one PDU and returns the response PDU.
func (c *Client) request(ctx context.Context, addr string, unit byte,
	pdu []byte) ([]byte, error) {

	cn := c.connFor(addr)
	cn.mu.Lock()
	defer cn.mu.Unlock()

	var lastErr error
	for attempt := 0; attempt <= c.retries; attempt++ {
		resp, err := cn.exchange(ctx, unit, pdu, c.timeout)
		if err == nil {
			return resp, nil
		}
		var ex *Exception
		if errors.As(err, &ex) {
			// The device answered. Retrying cannot change its mind.
			return nil, err
		}
		lastErr = err
		// A transport failure invalidates the socket: a half-read response
		// left in the buffer would be returned as the answer to the NEXT
		// request, which is a real reading attributed to the wrong register.
		cn.reset()
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}
	return nil, lastErr
}

func (cn *conn) reset() {
	if cn.sock != nil {
		_ = cn.sock.Close()
		cn.sock = nil
	}
}

func (cn *conn) exchange(ctx context.Context, unit byte, pdu []byte,
	timeout time.Duration) ([]byte, error) {

	if cn.sock == nil {
		d := net.Dialer{Timeout: timeout}
		sock, err := d.DialContext(ctx, "tcp", cn.addr)
		if err != nil {
			return nil, fmt.Errorf("connect %s: %w", cn.addr, err)
		}
		cn.sock = sock
		cn.txn = 0
	}

	cn.txn++
	txn := cn.txn
	frame := encodeADU(txn, unit, pdu)

	deadline := time.Now().Add(timeout)
	if dl, ok := ctx.Deadline(); ok && dl.Before(deadline) {
		deadline = dl
	}
	if err := cn.sock.SetDeadline(deadline); err != nil {
		return nil, err
	}
	if _, err := cn.sock.Write(frame); err != nil {
		return nil, fmt.Errorf("write %s: %w", cn.addr, err)
	}

	header := make([]byte, mbapLen)
	if _, err := io.ReadFull(cn.sock, header); err != nil {
		return nil, fmt.Errorf("read %s: %w", cn.addr, err)
	}
	total, ok := aduLength(header)
	if !ok {
		return nil, fmt.Errorf("%w: implausible length header from %s",
			ErrProtocol, cn.addr)
	}
	buf := make([]byte, total)
	copy(buf, header)
	if _, err := io.ReadFull(cn.sock, buf[mbapLen:]); err != nil {
		return nil, fmt.Errorf("read %s: %w", cn.addr, err)
	}

	gotTxn, gotUnit, respPDU, err := decodeADU(buf)
	if err != nil {
		return nil, err
	}
	if gotTxn != txn {
		// Modbus/TCP's only correlation is this 16-bit id. A stale reply
		// carries a real value for a register nobody asked about now.
		return nil, fmt.Errorf("%w: transaction %d, expected %d",
			ErrMismatch, gotTxn, txn)
	}
	if gotUnit != unit {
		return nil, fmt.Errorf("%w: unit %d, expected %d", ErrMismatch, gotUnit, unit)
	}
	return respPDU, nil
}

// ReadRegisters reads a run of holding or input registers.
func (c *Client) ReadRegisters(ctx context.Context, addr string, unit byte,
	space string, start, count uint16) ([]uint16, error) {

	fc := byte(fcReadInput)
	if space == "holding" {
		fc = fcReadHolding
	}
	pdu, err := readRequest(fc, start, count)
	if err != nil {
		return nil, err
	}
	resp, err := c.request(ctx, addr, unit, pdu)
	if err != nil {
		return nil, err
	}
	regs, err := parseReadRegisters(fc, resp)
	if err != nil {
		return nil, err
	}
	if len(regs) != int(count) {
		return nil, fmt.Errorf("%w: %d registers for a request of %d",
			ErrProtocol, len(regs), count)
	}
	return regs, nil
}

// ReadBits reads a run of coils or discrete inputs.
func (c *Client) ReadBits(ctx context.Context, addr string, unit byte,
	space string, start, count uint16) ([]bool, error) {

	fc := byte(fcReadDiscreteInputs)
	if space == "coil" {
		fc = fcReadCoils
	}
	pdu, err := readRequest(fc, start, count)
	if err != nil {
		return nil, err
	}
	resp, err := c.request(ctx, addr, unit, pdu)
	if err != nil {
		return nil, err
	}
	return parseReadBits(fc, resp, int(count))
}

// ReadIdentity asks a device who it is.
//
// FC43 is OPTIONAL in the standard and a great deal of gear rejects it. A
// rejection is "unknown", never a fault - refusing to poll a working meter
// because it does not implement an optional function would be the integration
// choosing its own convenience over the data.
func (c *Client) ReadIdentity(ctx context.Context, addr string, unit byte) (DeviceIdentity, error) {
	resp, err := c.request(ctx, addr, unit, deviceIDRequest())
	if err != nil {
		return DeviceIdentity{}, err
	}
	return parseDeviceID(resp)
}
