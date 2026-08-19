// Package modbus implements the Modbus/TCP client for electrical gear and
// field instruments.
//
// Modbus is the thinnest protocol the collector speaks, and almost every
// difficulty with it comes from what it does NOT carry:
//
//   - NO DISCOVERY. Nothing on the wire says what address 0x0020 means, what
//     scale applies, or which way round a 32-bit value is stored. The template
//     is the entire integration.
//
//   - NO QUALITY FLAG. Two bytes reading zero are indistinguishable from two
//     bytes that were never sampled. Real gear publishes a separate validity
//     point for exactly this reason, and the adapter gates on it.
//
//   - NO UNSOLICITED MESSAGING. No traps, no COV, no I-Am. The master polls,
//     and that is the whole protocol. A field device that appears to raise an
//     alarm on a real site is being polled by a BMS that raises it on its
//     behalf.
//
// What it does carry that matters: exception 0x0B, gateway target device
// failed to respond. That is the one thing no other protocol here can say -
// the network path is fine and the field device behind it is not.
package modbus

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
)

// Function codes.
const (
	fcReadCoils          = 0x01
	fcReadDiscreteInputs = 0x02
	fcReadHolding        = 0x03
	fcReadInput          = 0x04
	fcReadDeviceID       = 0x2B
	meiReadDeviceID      = 0x0E
)

// Exception codes.
const (
	exIllegalFunction  = 0x01
	exIllegalAddress   = 0x02
	exIllegalValue     = 0x03
	exSlaveFailure     = 0x04
	exAcknowledge      = 0x05
	exSlaveBusy        = 0x06
	exGatewayPath      = 0x0A
	exGatewayNoRespond = 0x0B
)

// Protocol limits. Asking for more is a bug in the master, and the device
// answers with exception 0x03 rather than a truncated result.
const (
	maxReadRegisters = 125
	maxReadBits      = 2000
	mbapLen          = 7
	maxPDU           = 253
)

var (
	// ErrShort is a truncated frame.
	ErrShort = errors.New("modbus: short frame")
	// ErrProtocol is a frame that does not parse as Modbus/TCP.
	ErrProtocol = errors.New("modbus: malformed response")
	// ErrMismatch is a reply that does not answer the request sent. On a
	// protocol whose only correlation is a 16-bit transaction id, this is the
	// difference between reading a device and reading whatever arrived.
	ErrMismatch = errors.New("modbus: response does not match the request")
)

// Exception is a device-reported exception.
//
// It is emphatically not a transport failure. A device answering 0x02 is
// alive and telling us the address does not exist, which means the TEMPLATE is
// wrong; 0x0B means the gateway is alive and the field device behind it is
// not. Collapsing those into one error hides whichever is happening.
type Exception struct {
	Function byte
	Code     byte
}

func (e *Exception) Error() string {
	return fmt.Sprintf("modbus exception %d (%s) on function 0x%02X",
		e.Code, exceptionName(e.Code), e.Function)
}

func exceptionName(code byte) string {
	switch code {
	case exIllegalFunction:
		return "illegal function"
	case exIllegalAddress:
		return "illegal data address"
	case exIllegalValue:
		return "illegal data value"
	case exSlaveFailure:
		return "slave device failure"
	case exAcknowledge:
		return "acknowledge"
	case exSlaveBusy:
		return "slave device busy"
	case exGatewayPath:
		return "gateway path unavailable"
	case exGatewayNoRespond:
		return "gateway target device failed to respond"
	default:
		return "unknown"
	}
}

// IsAddressFault reports an exception that means the template disagrees with
// the device: a wrong address, or a function the device does not implement.
func (e *Exception) IsAddressFault() bool {
	return e.Code == exIllegalAddress || e.Code == exIllegalFunction ||
		e.Code == exIllegalValue
}

// IsFieldDeviceDown reports the exception unique to a serial gateway: the
// gateway answered, and the unit behind it did not. The gateway is up, the
// instrument is off, and no timeout will tell you that.
func (e *Exception) IsFieldDeviceDown() bool {
	return e.Code == exGatewayNoRespond
}

// IsUnitAbsent reports that nothing is configured at this unit id.
func (e *Exception) IsUnitAbsent() bool { return e.Code == exGatewayPath }

// ------------------------------------------------------------- framing

// encodeADU wraps a PDU in an MBAP header.
func encodeADU(txn uint16, unit byte, pdu []byte) []byte {
	out := make([]byte, mbapLen+len(pdu))
	binary.BigEndian.PutUint16(out[0:], txn)
	binary.BigEndian.PutUint16(out[2:], 0) // protocol id: always 0
	binary.BigEndian.PutUint16(out[4:], uint16(len(pdu)+1))
	out[6] = unit
	copy(out[7:], pdu)
	return out
}

// decodeADU validates the MBAP header and returns the PDU.
//
// The transaction id and unit id are both checked by the caller. On a
// long-lived TCP connection a late reply to an abandoned request is otherwise
// indistinguishable from the answer to the current one, and the value it
// carries is real - just for the wrong register.
func decodeADU(buf []byte) (txn uint16, unit byte, pdu []byte, err error) {
	if len(buf) < mbapLen {
		return 0, 0, nil, ErrShort
	}
	txn = binary.BigEndian.Uint16(buf[0:])
	if proto := binary.BigEndian.Uint16(buf[2:]); proto != 0 {
		return 0, 0, nil, fmt.Errorf("%w: protocol id %d", ErrProtocol, proto)
	}
	length := int(binary.BigEndian.Uint16(buf[4:]))
	if length < 1 || length > maxPDU+1 {
		return 0, 0, nil, fmt.Errorf("%w: length field %d", ErrProtocol, length)
	}
	if len(buf) < mbapLen+length-1 {
		return 0, 0, nil, ErrShort
	}
	unit = buf[6]
	pdu = buf[mbapLen : mbapLen+length-1]
	return txn, unit, pdu, nil
}

// adduLength returns the total frame size implied by an MBAP header, so the
// reader knows how much to wait for. Modbus/TCP has no delimiter.
func aduLength(header []byte) (int, bool) {
	if len(header) < mbapLen {
		return 0, false
	}
	length := int(binary.BigEndian.Uint16(header[4:]))
	if length < 1 || length > maxPDU+1 {
		return 0, false
	}
	return mbapLen + length - 1, true
}

// ------------------------------------------------------------ requests

func readRequest(fc byte, addr, count uint16) ([]byte, error) {
	switch fc {
	case fcReadCoils, fcReadDiscreteInputs:
		if count < 1 || count > maxReadBits {
			return nil, fmt.Errorf("%w: %d bits requested", ErrProtocol, count)
		}
	case fcReadHolding, fcReadInput:
		if count < 1 || count > maxReadRegisters {
			return nil, fmt.Errorf("%w: %d registers requested", ErrProtocol, count)
		}
	default:
		return nil, fmt.Errorf("%w: function 0x%02X", ErrProtocol, fc)
	}
	pdu := make([]byte, 5)
	pdu[0] = fc
	binary.BigEndian.PutUint16(pdu[1:], addr)
	binary.BigEndian.PutUint16(pdu[3:], count)
	return pdu, nil
}

// deviceIDRequest asks for the basic identification objects: vendor, product
// code, revision. It is optional in the standard and plenty of gear rejects
// it, which the caller has to treat as "unknown", never as a fault.
func deviceIDRequest() []byte {
	return []byte{fcReadDeviceID, meiReadDeviceID, 0x01, 0x00}
}

// ----------------------------------------------------------- responses

// parseReadRegisters returns the register words from a read response.
func parseReadRegisters(fc byte, pdu []byte) ([]uint16, error) {
	if err := checkResponse(fc, pdu); err != nil {
		return nil, err
	}
	if len(pdu) < 2 {
		return nil, ErrShort
	}
	byteCount := int(pdu[1])
	if byteCount%2 != 0 || len(pdu) < 2+byteCount {
		return nil, fmt.Errorf("%w: byte count %d with %d bytes of payload",
			ErrProtocol, byteCount, len(pdu)-2)
	}
	out := make([]uint16, byteCount/2)
	for i := range out {
		out[i] = binary.BigEndian.Uint16(pdu[2+i*2:])
	}
	return out, nil
}

// parseReadBits unpacks a bit response. Bits are packed LSB-first within each
// byte, starting at the requested address.
func parseReadBits(fc byte, pdu []byte, count int) ([]bool, error) {
	if err := checkResponse(fc, pdu); err != nil {
		return nil, err
	}
	if len(pdu) < 2 {
		return nil, ErrShort
	}
	byteCount := int(pdu[1])
	if len(pdu) < 2+byteCount {
		return nil, ErrShort
	}
	if want := (count + 7) / 8; byteCount < want {
		return nil, fmt.Errorf("%w: %d bytes for %d bits", ErrProtocol, byteCount, count)
	}
	out := make([]bool, count)
	for i := 0; i < count; i++ {
		out[i] = pdu[2+i/8]&(1<<uint(i%8)) != 0
	}
	return out, nil
}

// DeviceIdentity is what FC43 reports, when a device implements it.
type DeviceIdentity struct {
	Vendor   string
	Product  string
	Revision string
}

func parseDeviceID(pdu []byte) (DeviceIdentity, error) {
	var id DeviceIdentity
	if err := checkResponse(fcReadDeviceID, pdu); err != nil {
		return id, err
	}
	// fc | mei | read code | conformity | more | next id | count | objects...
	if len(pdu) < 7 {
		return id, ErrShort
	}
	count := int(pdu[6])
	pos := 7
	for i := 0; i < count; i++ {
		if pos+2 > len(pdu) {
			return id, ErrShort
		}
		objID, objLen := pdu[pos], int(pdu[pos+1])
		pos += 2
		if pos+objLen > len(pdu) {
			return id, ErrShort
		}
		val := string(pdu[pos : pos+objLen])
		pos += objLen
		switch objID {
		case 0x00:
			id.Vendor = val
		case 0x01:
			id.Product = val
		case 0x02:
			id.Revision = val
		}
	}
	return id, nil
}

// checkResponse turns an exception PDU into an error.
func checkResponse(fc byte, pdu []byte) error {
	if len(pdu) < 1 {
		return ErrShort
	}
	if pdu[0] == fc|0x80 {
		if len(pdu) < 2 {
			return ErrShort
		}
		return &Exception{Function: fc, Code: pdu[1]}
	}
	if pdu[0] != fc {
		return fmt.Errorf("%w: answered function 0x%02X", ErrMismatch, pdu[0])
	}
	return nil
}

// ------------------------------------------------------------ decoding

// Word order for values wider than one register.
const (
	WordBig  = "big"  // high word first - the standard's own order
	WordSwap = "swap" // low word first  - Eaton PXG and many PLC gateways
)

// DecodeValue turns registers into a number.
//
// Word order is per template because it is per vendor, and getting it wrong
// does not fail: decoding an Eaton map with Schneider word order returns
// energy off by a factor of 65536. That is the most common Modbus integration
// bug there is, and it produces a number that charts perfectly well.
func DecodeValue(dtype string, regs []uint16, wordOrder string) (float64, error) {
	need := RegisterWidth(dtype)
	if len(regs) < need {
		return 0, fmt.Errorf("%w: %s needs %d registers, got %d",
			ErrShort, dtype, need, len(regs))
	}
	switch dtype {
	case "u16":
		return float64(regs[0]), nil
	case "s16":
		return float64(int16(regs[0])), nil
	case "u32":
		return float64(join32(regs, wordOrder)), nil
	case "s32":
		return float64(int32(join32(regs, wordOrder))), nil
	case "f32":
		return float64(math.Float32frombits(join32(regs, wordOrder))), nil
	default:
		return 0, fmt.Errorf("%w: unknown data type %q", ErrProtocol, dtype)
	}
}

func join32(regs []uint16, wordOrder string) uint32 {
	hi, lo := regs[0], regs[1]
	if wordOrder == WordSwap {
		hi, lo = regs[1], regs[0]
	}
	return uint32(hi)<<16 | uint32(lo)
}

// RegisterWidth is how many registers a data type occupies.
func RegisterWidth(dtype string) int {
	switch dtype {
	case "u32", "s32", "f32":
		return 2
	default:
		return 1
	}
}

// ------------------------------------------------------ block planning

// Block is one contiguous run of addresses to read in a single request.
type Block struct {
	Start uint16
	Count uint16
}

// PlanBlocks groups addresses into the fewest requests that never span a gap.
//
// This is not an optimisation, it is a correctness requirement. Real register
// maps are sparse, and a device answers a read that crosses an unimplemented
// address with exception 0x02 for the WHOLE request - so one blind span across
// a hole loses every point either side of it. Reading each point separately
// would be correct but turns a twenty-point meter into twenty round trips, and
// on a serial trunk that serialises at roughly 55 ms a transaction, that is the
// difference between a poll that completes and one that does not.
func PlanBlocks(addresses []uint16, widths map[uint16]int, maxCount int) []Block {
	if len(addresses) == 0 {
		return nil
	}
	sorted := append([]uint16(nil), addresses...)
	sortUint16(sorted)

	var blocks []Block
	start := sorted[0]
	end := start + uint16(width(widths, start)) // one past the last register

	for _, addr := range sorted[1:] {
		w := uint16(width(widths, addr))
		switch {
		case addr < end:
			// Overlapping or duplicate; extend if this point reaches further.
			if addr+w > end {
				end = addr + w
			}
		case addr == end && int(addr+w-start) <= maxCount:
			end = addr + w
		default:
			blocks = append(blocks, Block{Start: start, Count: end - start})
			start, end = addr, addr+w
		}
	}
	blocks = append(blocks, Block{Start: start, Count: end - start})
	return blocks
}

func width(widths map[uint16]int, addr uint16) int {
	if w, ok := widths[addr]; ok && w > 0 {
		return w
	}
	return 1
}

func sortUint16(a []uint16) {
	for i := 1; i < len(a); i++ {
		for j := i; j > 0 && a[j] < a[j-1]; j-- {
			a[j], a[j-1] = a[j-1], a[j]
		}
	}
}
