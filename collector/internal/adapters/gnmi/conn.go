package gnmi

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	gpb "github.com/openconfig/gnmi/proto/gnmi"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// ConnPool holds one gRPC connection per device address.
//
// gRPC connections are long-lived and multiplexed by design: one HTTP/2
// connection carries every RPC to that device, including a Subscribe stream
// that stays open for days. Dialling per poll would throw away the handshake
// and, on a switch, the session setup a device accounts for.
type ConnPool struct {
	log     *slog.Logger
	timeout time.Duration

	mu    sync.Mutex
	conns map[string]*grpc.ClientConn // address -> connection
	byEP  map[string]string           // endpoint id -> address
	creds map[string]credential
}

type credential struct {
	username string
	password string
}

func NewConnPool(timeout time.Duration, log *slog.Logger) *ConnPool {
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	return &ConnPool{
		log: log, timeout: timeout,
		conns: make(map[string]*grpc.ClientConn),
		byEP:  make(map[string]string),
		creds: make(map[string]credential),
	}
}

// Client returns a gNMI stub for a target, dialling if needed.
func (p *ConnPool) Client(ctx context.Context, endpointID string,
	tgt target) (gpb.GNMIClient, error) {

	p.mu.Lock()
	if conn, ok := p.conns[tgt.addr]; ok {
		p.byEP[endpointID] = tgt.addr
		p.mu.Unlock()
		return gpb.NewGNMIClient(conn), nil
	}
	p.mu.Unlock()

	var creds credentials.TransportCredentials
	if tgt.tls {
		// A lab device with a self-signed certificate is the norm, and the
		// verification decision belongs per endpoint rather than to the
		// process - the same reasoning as the Redfish adapter.
		creds = credentials.NewTLS(&tls.Config{InsecureSkipVerify: true}) //nolint:gosec
	} else {
		creds = insecure.NewCredentials()
	}

	dialCtx, cancel := context.WithTimeout(ctx, p.timeout)
	defer cancel()

	conn, err := grpc.DialContext(dialCtx, tgt.addr, //nolint:staticcheck // NewClient needs grpc 1.63+ semantics we do not want yet
		grpc.WithTransportCredentials(creds),
		grpc.WithBlock(), //nolint:staticcheck // a dial that has not connected is not a usable client
	)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", tgt.addr, err)
	}

	p.mu.Lock()
	// Another goroutine may have dialled the same address meanwhile.
	if existing, ok := p.conns[tgt.addr]; ok {
		p.mu.Unlock()
		_ = conn.Close()
		return gpb.NewGNMIClient(existing), nil
	}
	p.conns[tgt.addr] = conn
	p.byEP[endpointID] = tgt.addr
	p.mu.Unlock()

	p.log.Info("gnmi connected", "address", tgt.addr, "tls", tgt.tls)
	return gpb.NewGNMIClient(conn), nil
}

// SetCredential records the username and password for an address, applied as
// gRPC metadata on every call. This is how every gNMI implementation carries
// device credentials - there is no auth message in the protocol.
func (p *ConnPool) SetCredential(addr, username, password string) {
	p.mu.Lock()
	p.creds[addr] = credential{username: username, password: password}
	p.mu.Unlock()
}

// WithAuth attaches credentials for an address, if any are known.
func (p *ConnPool) WithAuth(ctx context.Context, addr string) context.Context {
	p.mu.Lock()
	c, ok := p.creds[addr]
	p.mu.Unlock()
	if !ok || c.username == "" {
		return ctx
	}
	return metadata.AppendToOutgoingContext(ctx, "username", c.username,
		"password", c.password)
}

// ForgetEndpoint drops the connection an endpoint was using, if no other
// endpoint shares it.
func (p *ConnPool) ForgetEndpoint(endpointID string) {
	p.mu.Lock()
	addr, ok := p.byEP[endpointID]
	delete(p.byEP, endpointID)
	if !ok {
		p.mu.Unlock()
		return
	}
	for _, a := range p.byEP {
		if a == addr {
			// Still in use by another endpoint on the same service.
			p.mu.Unlock()
			return
		}
	}
	conn := p.conns[addr]
	delete(p.conns, addr)
	p.mu.Unlock()
	if conn != nil {
		_ = conn.Close()
	}
}

func (p *ConnPool) Close() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, c := range p.conns {
		_ = c.Close()
	}
	p.conns = make(map[string]*grpc.ClientConn)
	p.byEP = make(map[string]string)
	return nil
}

// Connections reports how many are held, for the health gauge.
func (p *ConnPool) Connections() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.conns)
}

// classify maps a gRPC status onto the health vocabulary.
//
// The codes carry real distinctions worth preserving. Unauthenticated means
// the credential is wrong and retrying it locks accounts on real gear;
// Unimplemented means the device does not serve that subtree, which is a
// mapping problem, not an outage.
func classify(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return fmt.Errorf("%w: %v", models.ErrTimeout, err)
	}
	st, ok := status.FromError(err)
	if !ok {
		return fmt.Errorf("%w: %v", models.ErrUnreachable, err)
	}
	switch st.Code() {
	case codes.Unauthenticated, codes.PermissionDenied:
		return fmt.Errorf("%w: %s", models.ErrAuth, st.Message())
	case codes.DeadlineExceeded:
		return fmt.Errorf("%w: %s", models.ErrTimeout, st.Message())
	case codes.Unavailable:
		return fmt.Errorf("%w: %s", models.ErrUnreachable, st.Message())
	case codes.Unimplemented, codes.InvalidArgument, codes.NotFound:
		return fmt.Errorf("%w: %s - the device does not serve this path",
			models.ErrConfig, st.Message())
	case codes.ResourceExhausted:
		return fmt.Errorf("%w: %s", models.ErrProtocolStatus, st.Message())
	default:
		return fmt.Errorf("%w: %s (%s)", models.ErrProtocolStatus,
			st.Message(), st.Code())
	}
}
