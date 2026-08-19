// Package bacnet implements the BACnet/IP client the collector uses for
// mechanical and electrical plant.
//
// This file is the wire codec: BVLL, NPDU and APDU per ASHRAE 135. It is
// written from the standard rather than against one device's quirks, because
// the same collector has to talk to Trane, Johnson, Siemens and Schneider
// controllers that agree on very little except the encoding.
//
// Three facts about BACnet drive everything here and are worth stating once:
//
//   - THE OBJECT IDENTIFIER IS A PACKED 32-BIT WORD: 10 bits of type, 22 bits
//     of instance. It is not two fields on the wire.
//
//   - A TAG IS EITHER APPLICATION OR CONTEXT. An application tag says what the
//     value IS (real, enumerated, character string). A context tag says which
//     PARAMETER it is, and the reader must already know the type. Confusing
//     the two is the single most common way a BACnet parser silently produces
//     wrong numbers rather than an error.
//
//   - PROPERTY VALUES ARE BRACKETED by opening and closing context tags, so a
//     value the reader does not understand can be skipped without losing sync
//     with the rest of the response. That property is what makes partial
//     decoding safe, and this decoder relies on it.
package bacnet

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
)

// BVLL
const (
	bvllType             = 0x81
	bvlcOriginalUnicast  = 0x0A
	bvlcOriginalBroadcst = 0x0B
	bvlcForwardedNPDU    = 0x04
)

// APDU PDU types
const (
	pduConfirmedRequest   = 0
	pduUnconfirmedRequest = 1
	pduSimpleAck          = 2
	pduComplexAck         = 3
	pduSegmentAck         = 4
	pduError              = 5
	pduReject             = 6
	pduAbort              = 7
)

// Service choices used here.
const (
	svcConfirmedCOVNotification = 1
	svcSubscribeCOV             = 5
	svcReadProperty             = 12
	svcReadPropertyMultiple     = 14

	svcUnconfirmedCOVNotification = 2
	svcWhoIs                      = 8
	svcIAm                        = 0
)

// Object types.
const (
	ObjAnalogInput  = 0
	ObjAnalogOutput = 1
	ObjAnalogValue  = 2
	ObjBinaryInput  = 3
	ObjBinaryOutput = 4
	ObjBinaryValue  = 5
	ObjDevice       = 8
	ObjMultiStateIn = 13
)

// Property identifiers.
const (
	PropObjectIdentifier = 75
	PropObjectList       = 76
	PropObjectName       = 77
	PropObjectType       = 79
	PropPresentValue     = 85
	PropReliability      = 103
	PropStatusFlags      = 111
	PropUnits            = 117
	PropDescription      = 28
	// PropAll is the pseudo-property "read everything". Deliberately unused in
	// the poll path: on a 233-object panel it returns tens of kilobytes per
	// object and forces segmentation for data we then throw away.
	PropAll = 8
)

// Application tag numbers.
const (
	tagNull        = 0
	tagBoolean     = 1
	tagUnsigned    = 2
	tagSigned      = 3
	tagReal        = 4
	tagDouble      = 5
	tagOctetString = 6
	tagCharString  = 7
	tagBitString   = 8
	tagEnumerated  = 9
	tagDate        = 10
	tagTime        = 11
	tagObjectID    = 12
)

var (
	// ErrShort is a truncated or malformed frame.
	ErrShort = errors.New("bacnet: truncated frame")
	// ErrNotBACnet is a datagram that is not BACnet/IP at all.
	ErrNotBACnet = errors.New("bacnet: not a BVLL frame")
	// ErrUnexpected is a well-formed frame that is not what was asked for.
	ErrUnexpected = errors.New("bacnet: unexpected response")
)

// ObjectID is a BACnet object identifier.
type ObjectID struct {
	Type     uint16
	Instance uint32
}

func (o ObjectID) String() string {
	return fmt.Sprintf("%s:%d", objectTypeName(o.Type), o.Instance)
}

func (o ObjectID) packed() uint32 {
	return (uint32(o.Type&0x3FF) << 22) | (o.Instance & 0x3FFFFF)
}

func unpackObjectID(v uint32) ObjectID {
	return ObjectID{Type: uint16(v >> 22), Instance: v & 0x3FFFFF}
}

func objectTypeName(t uint16) string {
	switch t {
	case ObjAnalogInput:
		return "analog-input"
	case ObjAnalogOutput:
		return "analog-output"
	case ObjAnalogValue:
		return "analog-value"
	case ObjBinaryInput:
		return "binary-input"
	case ObjBinaryOutput:
		return "binary-output"
	case ObjBinaryValue:
		return "binary-value"
	case ObjDevice:
		return "device"
	case ObjMultiStateIn:
		return "multi-state-input"
	default:
		return fmt.Sprintf("type-%d", t)
	}
}

// Address identifies a BACnet device. A device behind an MS/TP router has no
// IP of its own: the router's IP carries the packet and (Net, MAC) inside the
// NPDU says which device on the trunk it is. Getting this wrong is how an
// integration ends up seeing one device where there are eighteen.
type Address struct {
	IP  string
	Net uint16 // 0 = local network, no routing
	MAC []byte // MS/TP MAC, only meaningful with Net != 0
}

// Routed reports whether this address sits behind a router.
func (a Address) Routed() bool { return a.Net != 0 && len(a.MAC) > 0 }

// ------------------------------------------------------------ encoding

type encoder struct{ buf []byte }

func (e *encoder) bytes() []byte { return e.buf }

func (e *encoder) raw(b ...byte) { e.buf = append(e.buf, b...) }

// tagHeader writes a tag header. Tag numbers above 14 use the extended form,
// which real devices do emit for proprietary properties.
func (e *encoder) tagHeader(tagNum byte, context bool, lenVal byte) {
	var b byte
	if tagNum <= 14 {
		b = tagNum << 4
	} else {
		b = 0xF0
	}
	if context {
		b |= 0x08
	}
	b |= lenVal & 0x07
	e.buf = append(e.buf, b)
	if tagNum > 14 {
		e.buf = append(e.buf, tagNum)
	}
}

// tagged writes a tag with a data payload, using the extended-length form when
// the payload does not fit the 3-bit length field.
func (e *encoder) tagged(tagNum byte, context bool, data []byte) {
	n := len(data)
	if n <= 4 {
		e.tagHeader(tagNum, context, byte(n))
	} else {
		e.tagHeader(tagNum, context, 5)
		switch {
		case n <= 253:
			e.buf = append(e.buf, byte(n))
		case n <= 0xFFFF:
			e.buf = append(e.buf, 254, byte(n>>8), byte(n))
		default:
			e.buf = append(e.buf, 255, byte(n>>24), byte(n>>16), byte(n>>8), byte(n))
		}
	}
	e.buf = append(e.buf, data...)
}

func (e *encoder) ctxUint(tagNum byte, v uint32) {
	e.tagged(tagNum, true, minUint(v))
}

func (e *encoder) ctxObjectID(tagNum byte, id ObjectID) {
	var b [4]byte
	binary.BigEndian.PutUint32(b[:], id.packed())
	e.tagged(tagNum, true, b[:])
}

func (e *encoder) open(tagNum byte)  { e.tagHeader(tagNum, true, 6) }
func (e *encoder) close(tagNum byte) { e.tagHeader(tagNum, true, 7) }

func (e *encoder) appUint(v uint32) { e.tagged(tagUnsigned, false, minUint(v)) }

// minUint encodes an unsigned in the fewest bytes. Zero is one zero byte, not
// zero bytes: a zero-length unsigned is legal to decode but several devices
// reject it on input.
func minUint(v uint32) []byte {
	switch {
	case v <= 0xFF:
		return []byte{byte(v)}
	case v <= 0xFFFF:
		return []byte{byte(v >> 8), byte(v)}
	case v <= 0xFFFFFF:
		return []byte{byte(v >> 16), byte(v >> 8), byte(v)}
	default:
		return []byte{byte(v >> 24), byte(v >> 16), byte(v >> 8), byte(v)}
	}
}

// ------------------------------------------------------------- framing

// frame wraps an APDU in an NPDU and a BVLL header.
//
// dest carries the routing: when it names a remote network the NPDU gets
// DNET/DLEN/DADR and a hop count, which is what makes an MS/TP device behind
// a router addressable at all.
func frame(apdu []byte, dest Address, expectsReply, broadcast bool) []byte {
	npdu := []byte{0x01, 0x00} // version 1, control filled in below
	control := byte(0)
	if expectsReply {
		control |= 0x04
	}
	if dest.Routed() {
		control |= 0x20 // DNET/DLEN/DADR present
	}
	npdu[1] = control

	if dest.Routed() {
		npdu = append(npdu, byte(dest.Net>>8), byte(dest.Net), byte(len(dest.MAC)))
		npdu = append(npdu, dest.MAC...)
		npdu = append(npdu, 255) // hop count
	}
	npdu = append(npdu, apdu...)

	length := 4 + len(npdu)
	fn := byte(bvlcOriginalUnicast)
	if broadcast {
		fn = bvlcOriginalBroadcst
	}
	out := []byte{bvllType, fn, byte(length >> 8), byte(length)}
	return append(out, npdu...)
}

// npduInfo is what the network layer says about a received frame.
type npduInfo struct {
	SrcNet uint16
	SrcMAC []byte
	APDU   []byte
}

// unframe strips BVLL and NPDU.
//
// The source specifier matters as much as the payload: a reply from behind a
// router carries SNET/SADR, and without reading them every device on a trunk
// looks like the router itself.
func unframe(data []byte) (npduInfo, error) {
	var out npduInfo
	if len(data) < 4 {
		return out, ErrShort
	}
	if data[0] != bvllType {
		return out, ErrNotBACnet
	}
	length := int(data[2])<<8 | int(data[3])
	if length > len(data) || length < 4 {
		return out, ErrShort
	}
	body := data[4:length]

	switch data[1] {
	case bvlcOriginalUnicast, bvlcOriginalBroadcst:
	case bvlcForwardedNPDU:
		// A BBMD forwards with the original source address prepended.
		if len(body) < 6 {
			return out, ErrShort
		}
		body = body[6:]
	default:
		return out, fmt.Errorf("%w: bvlc function 0x%02x", ErrUnexpected, data[1])
	}

	if len(body) < 2 || body[0] != 0x01 {
		return out, ErrShort
	}
	control := body[1]
	pos := 2

	if control&0x20 != 0 { // DNET/DLEN/DADR
		if len(body) < pos+3 {
			return out, ErrShort
		}
		dlen := int(body[pos+2])
		pos += 3 + dlen
	}
	if control&0x08 != 0 { // SNET/SLEN/SADR
		if len(body) < pos+3 {
			return out, ErrShort
		}
		out.SrcNet = uint16(body[pos])<<8 | uint16(body[pos+1])
		slen := int(body[pos+2])
		pos += 3
		if len(body) < pos+slen {
			return out, ErrShort
		}
		out.SrcMAC = append([]byte(nil), body[pos:pos+slen]...)
		pos += slen
	}
	if control&0x20 != 0 {
		pos++ // hop count
	}
	if control&0x80 != 0 {
		// A network-layer message (Who-Is-Router and friends) carries no APDU.
		return out, fmt.Errorf("%w: network layer message", ErrUnexpected)
	}
	if pos > len(body) {
		return out, ErrShort
	}
	out.APDU = body[pos:]
	return out, nil
}

// ------------------------------------------------------------ requests

// readPropertyRequest builds a ReadProperty confirmed request.
func readPropertyRequest(invokeID byte, obj ObjectID, prop uint32) []byte {
	e := &encoder{}
	// max-segments = unspecified, max-APDU = 1476 (the Ethernet-sized class).
	// Asking for more than the link can carry is how a large object list turns
	// into an abort instead of a reply.
	e.raw(pduConfirmedRequest<<4, 0x05, invokeID, svcReadProperty)
	e.ctxObjectID(0, obj)
	e.ctxUint(1, prop)
	return e.bytes()
}

// readPropertyIndexRequest reads one element of an array property. Element 0
// of object-list is its LENGTH, which is how a client sizes the list without
// pulling the whole thing in one segmented response.
func readPropertyIndexRequest(invokeID byte, obj ObjectID, prop, index uint32) []byte {
	e := &encoder{}
	e.raw(pduConfirmedRequest<<4, 0x05, invokeID, svcReadProperty)
	e.ctxObjectID(0, obj)
	e.ctxUint(1, prop)
	e.ctxUint(2, index)
	return e.bytes()
}

// ReadSpec is one object and the properties wanted from it.
type ReadSpec struct {
	Object ObjectID
	Props  []uint32
}

// readPropertyMultipleRequest batches several objects into one request.
//
// This is the difference between a poll that finishes and one that does not: a
// panel with 200 points answered one ReadProperty at a time is 200 round trips
// against a controller that serialises them.
func readPropertyMultipleRequest(invokeID byte, specs []ReadSpec) []byte {
	e := &encoder{}
	e.raw(pduConfirmedRequest<<4, 0x05, invokeID, svcReadPropertyMultiple)
	for _, s := range specs {
		e.ctxObjectID(0, s.Object)
		e.open(1)
		for _, p := range s.Props {
			e.ctxUint(0, p)
		}
		e.close(1)
	}
	return e.bytes()
}

// whoIsRequest builds a Who-Is, optionally limited to an instance range.
func whoIsRequest(low, high uint32, ranged bool) []byte {
	e := &encoder{}
	e.raw(pduUnconfirmedRequest<<4, svcWhoIs)
	if ranged {
		e.ctxUint(0, low)
		e.ctxUint(1, high)
	}
	return e.bytes()
}

// subscribeCOVRequest asks a device to push changes for one object.
func subscribeCOVRequest(invokeID byte, processID uint32, obj ObjectID,
	confirmed bool, lifetimeSec uint32) []byte {
	e := &encoder{}
	e.raw(pduConfirmedRequest<<4, 0x05, invokeID, svcSubscribeCOV)
	e.ctxUint(0, processID)
	e.ctxObjectID(1, obj)
	// Both are omitted for a cancellation; present together otherwise.
	e.tagged(2, true, []byte{boolByte(confirmed)})
	e.ctxUint(3, lifetimeSec)
	return e.bytes()
}

func boolByte(b bool) byte {
	if b {
		return 1
	}
	return 0
}

// ----------------------------------------------------------- responses

type apduKind int

const (
	kindComplexAck apduKind = iota
	kindSimpleAck
	kindError
	kindReject
	kindAbort
	kindUnconfirmed
)

type apdu struct {
	Kind     apduKind
	InvokeID byte
	Service  byte
	Payload  []byte
}

// parseAPDU reads the APDU header.
func parseAPDU(b []byte) (apdu, error) {
	var a apdu
	if len(b) < 2 {
		return a, ErrShort
	}
	switch (b[0] >> 4) & 0x0F {
	case pduComplexAck:
		if len(b) < 3 {
			return a, ErrShort
		}
		if b[0]&0x08 != 0 {
			// Segmented. Not supported, and it must be reported rather than
			// silently decoded as a partial value: half an object list looks
			// exactly like a short one.
			return a, fmt.Errorf("%w: segmented response", ErrUnexpected)
		}
		a = apdu{Kind: kindComplexAck, InvokeID: b[1], Service: b[2], Payload: b[3:]}
	case pduSimpleAck:
		if len(b) < 3 {
			return a, ErrShort
		}
		a = apdu{Kind: kindSimpleAck, InvokeID: b[1], Service: b[2]}
	case pduError:
		if len(b) < 3 {
			return a, ErrShort
		}
		a = apdu{Kind: kindError, InvokeID: b[1], Service: b[2], Payload: b[3:]}
	case pduReject:
		a = apdu{Kind: kindReject, InvokeID: b[1], Payload: b[2:]}
	case pduAbort:
		a = apdu{Kind: kindAbort, InvokeID: b[1], Payload: b[2:]}
	case pduUnconfirmedRequest:
		a = apdu{Kind: kindUnconfirmed, Service: b[1], Payload: b[2:]}
	default:
		return a, fmt.Errorf("%w: pdu type %d", ErrUnexpected, (b[0]>>4)&0x0F)
	}
	return a, nil
}

// APDUError is a device-reported error, reject or abort.
//
// It is deliberately distinct from a transport failure: "unknown object" means
// the point is gone and the mapping is stale, whereas a timeout means the
// device is unreachable. Treating them alike either hides a dead controller or
// raises one for a renamed point.
type APDUError struct {
	Kind   apduKind
	Class  uint32
	Code   uint32
	Reason uint32
}

func (e *APDUError) Error() string {
	switch e.Kind {
	case kindReject:
		return fmt.Sprintf("bacnet reject: reason %d", e.Reason)
	case kindAbort:
		return fmt.Sprintf("bacnet abort: reason %d", e.Reason)
	default:
		return fmt.Sprintf("bacnet error: class %d code %d", e.Class, e.Code)
	}
}

// IsUnknownObject reports the error that means "this point does not exist".
func (e *APDUError) IsUnknownObject() bool {
	return e.Kind == kindError && e.Class == 1 && e.Code == 31
}

// IsUnknownProperty reports "this object exists but not that property".
func (e *APDUError) IsUnknownProperty() bool {
	return e.Kind == kindError && e.Class == 2 && e.Code == 32
}

func errorFrom(a apdu) *APDUError {
	switch a.Kind {
	case kindReject, kindAbort:
		var reason uint32
		if len(a.Payload) > 0 {
			reason = uint32(a.Payload[0])
		}
		return &APDUError{Kind: a.Kind, Reason: reason}
	default:
		d := &decoder{buf: a.Payload}
		class, _ := d.appUint()
		code, _ := d.appUint()
		return &APDUError{Kind: kindError, Class: class, Code: code}
	}
}

// ------------------------------------------------------------ decoding

type decoder struct {
	buf []byte
	pos int
}

type tag struct {
	Num     byte
	Context bool
	Opening bool
	Closing bool
	Len     int
}

func (d *decoder) eof() bool { return d.pos >= len(d.buf) }

// peekTag reads a tag header without consuming it.
func (d *decoder) peekTag() (tag, int, error) {
	p := d.pos
	if p >= len(d.buf) {
		return tag{}, 0, ErrShort
	}
	b := d.buf[p]
	t := tag{Num: (b >> 4) & 0x0F, Context: b&0x08 != 0}
	lenEnc := b & 0x07
	p++
	if t.Num == 0x0F { // extended tag number
		if p >= len(d.buf) {
			return tag{}, 0, ErrShort
		}
		t.Num = d.buf[p]
		p++
	}
	switch {
	case t.Context && lenEnc == 6:
		t.Opening = true
		return t, p, nil
	case t.Context && lenEnc == 7:
		t.Closing = true
		return t, p, nil
	case lenEnc == 5:
		if p >= len(d.buf) {
			return tag{}, 0, ErrShort
		}
		ext := d.buf[p]
		p++
		switch ext {
		case 254:
			if p+2 > len(d.buf) {
				return tag{}, 0, ErrShort
			}
			t.Len = int(d.buf[p])<<8 | int(d.buf[p+1])
			p += 2
		case 255:
			if p+4 > len(d.buf) {
				return tag{}, 0, ErrShort
			}
			t.Len = int(binary.BigEndian.Uint32(d.buf[p : p+4]))
			p += 4
		default:
			t.Len = int(ext)
		}
	default:
		t.Len = int(lenEnc)
	}
	return t, p, nil
}

// nextTag consumes a tag header and returns it with its data.
func (d *decoder) nextTag() (tag, []byte, error) {
	t, p, err := d.peekTag()
	if err != nil {
		return t, nil, err
	}
	if t.Opening || t.Closing {
		d.pos = p
		return t, nil, nil
	}
	if p+t.Len > len(d.buf) {
		return t, nil, ErrShort
	}
	data := d.buf[p : p+t.Len]
	d.pos = p + t.Len
	return t, data, nil
}

// appUint reads an application-tagged unsigned.
func (d *decoder) appUint() (uint32, error) {
	_, data, err := d.nextTag()
	if err != nil {
		return 0, err
	}
	return beUint(data), nil
}

func beUint(b []byte) uint32 {
	var v uint32
	for _, c := range b {
		v = v<<8 | uint32(c)
	}
	return v
}

// skipToClose advances past everything up to and including the closing tag
// that matches num.
//
// This is what makes an unknown value harmless. A device that returns a
// proprietary structure for one property must not cost us the other twenty in
// the same response.
func (d *decoder) skipToClose(num byte) error {
	depth := 0
	for !d.eof() {
		t, _, err := d.nextTag()
		if err != nil {
			return err
		}
		switch {
		case t.Opening && t.Num == num:
			depth++
		case t.Closing && t.Num == num:
			if depth == 0 {
				return nil
			}
			depth--
		}
	}
	return ErrShort
}

// Value is a decoded BACnet application value.
//
// Kind is kept alongside the number because "present-value 1" from a binary
// input is a STATE, not a measurement: rendering it as 1.0 on a temperature
// chart is the sort of thing that survives review and confuses an operator at
// three in the morning.
type Value struct {
	Kind   byte
	Num    float64
	Text   string
	Bits   uint32
	Object ObjectID
	IsNull bool
}

// decodeValue reads one application-tagged value.
func (d *decoder) decodeValue() (Value, error) {
	t, p, err := d.peekTag()
	if err != nil {
		return Value{}, err
	}
	if t.Context {
		return Value{}, fmt.Errorf("%w: context tag %d where a value was expected",
			ErrUnexpected, t.Num)
	}

	// A Boolean carries its value in the length field and has NO data. Some
	// implementations (the simulator among them) write a data byte as well;
	// the extra byte is skipped by the bracket scan rather than parsed, which
	// is why values live inside opening/closing tags in the first place.
	if t.Num == tagBoolean {
		d.pos = p
		return Value{Kind: tagBoolean, Num: float64(t.Len & 0x01)}, nil
	}

	_, data, err := d.nextTag()
	if err != nil {
		return Value{}, err
	}
	v := Value{Kind: t.Num}
	switch t.Num {
	case tagNull:
		v.IsNull = true
	case tagUnsigned, tagEnumerated:
		v.Num = float64(beUint(data))
	case tagSigned:
		v.Num = float64(beInt(data))
	case tagReal:
		if len(data) != 4 {
			return v, ErrShort
		}
		v.Num = float64(math.Float32frombits(binary.BigEndian.Uint32(data)))
	case tagDouble:
		if len(data) != 8 {
			return v, ErrShort
		}
		v.Num = math.Float64frombits(binary.BigEndian.Uint64(data))
	case tagCharString:
		// First byte is the character-set indicator. UTF-8 (0) and the
		// ANSI X3.4 alias are byte-compatible; anything else is returned raw
		// rather than mangled by a wrong assumption.
		if len(data) > 0 {
			v.Text = string(data[1:])
		}
	case tagBitString:
		if len(data) > 1 {
			unused := int(data[0])
			bits := uint32(0)
			for _, c := range data[1:] {
				bits = bits<<8 | uint32(c)
			}
			v.Bits = bits >> uint(unused%8)
		}
	case tagObjectID:
		if len(data) != 4 {
			return v, ErrShort
		}
		v.Object = unpackObjectID(binary.BigEndian.Uint32(data))
	case tagOctetString, tagDate, tagTime:
		// Carried through as raw text so nothing is silently lost.
		v.Text = fmt.Sprintf("%x", data)
	default:
		v.Text = fmt.Sprintf("%x", data)
	}
	return v, nil
}

func beInt(b []byte) int64 {
	if len(b) == 0 {
		return 0
	}
	v := int64(int8(b[0]))
	for _, c := range b[1:] {
		v = v<<8 | int64(c)
	}
	return v
}

// parseReadPropertyAck decodes a ReadProperty ComplexAck into its values.
// An array property returns several values, so the result is a slice.
func parseReadPropertyAck(payload []byte) (ObjectID, uint32, []Value, error) {
	d := &decoder{buf: payload}

	t, data, err := d.nextTag()
	if err != nil {
		return ObjectID{}, 0, nil, err
	}
	if !t.Context || t.Num != 0 || len(data) != 4 {
		return ObjectID{}, 0, nil, fmt.Errorf("%w: no object identifier", ErrUnexpected)
	}
	obj := unpackObjectID(binary.BigEndian.Uint32(data))

	t, data, err = d.nextTag()
	if err != nil {
		return obj, 0, nil, err
	}
	if !t.Context || t.Num != 1 {
		return obj, 0, nil, fmt.Errorf("%w: no property identifier", ErrUnexpected)
	}
	prop := beUint(data)

	// An echoed array index is optional and must not be mistaken for the
	// opening tag of the value.
	t, _, err = d.peekTag()
	if err != nil {
		return obj, prop, nil, err
	}
	if t.Context && t.Num == 2 && !t.Opening {
		if _, _, err := d.nextTag(); err != nil {
			return obj, prop, nil, err
		}
	}

	if _, _, err := d.nextTag(); err != nil { // opening [3]
		return obj, prop, nil, err
	}
	values, err := d.valuesUntilClose(3)
	return obj, prop, values, err
}

// valuesUntilClose reads application values until the matching closing tag.
func (d *decoder) valuesUntilClose(num byte) ([]Value, error) {
	var out []Value
	for {
		t, _, err := d.peekTag()
		if err != nil {
			return out, err
		}
		if t.Closing && t.Num == num {
			_, _, _ = d.nextTag()
			return out, nil
		}
		if t.Opening {
			// A constructed value we do not model. Skip it whole rather than
			// guess at its shape.
			if _, _, err := d.nextTag(); err != nil {
				return out, err
			}
			if err := d.skipToClose(t.Num); err != nil {
				return out, err
			}
			continue
		}
		v, err := d.decodeValue()
		if err != nil {
			// Resync on the bracket instead of abandoning the response.
			if serr := d.skipToClose(num); serr != nil {
				return out, err
			}
			return out, nil
		}
		out = append(out, v)
	}
}

// RPMResult is one property result inside a ReadPropertyMultiple ack.
type RPMResult struct {
	Object ObjectID
	Prop   uint32
	Values []Value
	Err    *APDUError // per-property, e.g. one missing point out of forty
}

// parseRPMAck decodes a ReadPropertyMultiple ComplexAck.
//
// Per-property errors are returned as results with Err set, not as a failure
// of the whole read. One renamed point must not blind the collector to the
// other thirty-nine on the same panel.
func parseRPMAck(payload []byte) ([]RPMResult, error) {
	d := &decoder{buf: payload}
	var out []RPMResult

	for !d.eof() {
		t, data, err := d.nextTag()
		if err != nil {
			return out, err
		}
		if !t.Context || t.Num != 0 || len(data) != 4 {
			return out, fmt.Errorf("%w: expected an object identifier", ErrUnexpected)
		}
		obj := unpackObjectID(binary.BigEndian.Uint32(data))

		t, _, err = d.nextTag() // opening [1]
		if err != nil {
			return out, err
		}
		if !t.Opening || t.Num != 1 {
			return out, fmt.Errorf("%w: expected the result list", ErrUnexpected)
		}

		for {
			t, _, err := d.peekTag()
			if err != nil {
				return out, err
			}
			if t.Closing && t.Num == 1 {
				_, _, _ = d.nextTag()
				break
			}
			if !t.Opening || t.Num != 2 {
				return out, fmt.Errorf("%w: expected a result entry", ErrUnexpected)
			}
			if _, _, err := d.nextTag(); err != nil { // opening [2]
				return out, err
			}

			res := RPMResult{Object: obj}

			_, pdata, err := d.nextTag() // [0] property identifier
			if err != nil {
				return out, err
			}
			res.Prop = beUint(pdata)

			// Optional [1] array index.
			if t2, _, err := d.peekTag(); err == nil && t2.Context && t2.Num == 1 &&
				!t2.Opening && !t2.Closing {
				if _, _, err := d.nextTag(); err != nil {
					return out, err
				}
			}

			t2, _, err := d.peekTag()
			if err != nil {
				return out, err
			}
			switch {
			case t2.Opening && t2.Num == 4: // property value
				if _, _, err := d.nextTag(); err != nil {
					return out, err
				}
				vals, err := d.valuesUntilClose(4)
				if err != nil {
					return out, err
				}
				res.Values = vals
			case t2.Opening && t2.Num == 5: // property access error
				if _, _, err := d.nextTag(); err != nil {
					return out, err
				}
				class, _ := d.appUint()
				code, _ := d.appUint()
				res.Err = &APDUError{Kind: kindError, Class: class, Code: code}
				if err := d.skipToClose(5); err != nil {
					return out, err
				}
			default:
				return out, fmt.Errorf("%w: neither a value nor an error", ErrUnexpected)
			}

			if err := d.skipToClose(2); err != nil {
				return out, err
			}
			out = append(out, res)
		}
	}
	return out, nil
}

// parseIAm decodes an I-Am, which is how a device announces its instance
// number - the identity BACnet actually uses, independent of its IP.
func parseIAm(payload []byte) (ObjectID, uint32, uint32, error) {
	d := &decoder{buf: payload}
	v, err := d.decodeValue()
	if err != nil {
		return ObjectID{}, 0, 0, err
	}
	if v.Kind != tagObjectID {
		return ObjectID{}, 0, 0, fmt.Errorf("%w: no device identifier", ErrUnexpected)
	}
	maxAPDU, _ := d.appUint()
	seg, _ := d.decodeValue()
	vendor, _ := d.appUint()
	_ = seg
	return v.Object, maxAPDU, vendor, nil
}
