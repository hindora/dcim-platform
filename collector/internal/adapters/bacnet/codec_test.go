package bacnet

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"testing"
)

// These tests run the codec against vectors produced by the SIMULATOR's own
// BACnet encoder - an independent implementation of ASHRAE 135. A codec tested
// only against itself proves nothing, because every encoder agrees with its
// own decoder.
//
// Regenerate with:
//
//	python contracts/tools/gen_bacnet_vectors.py

type wantValue struct {
	Kind     string  `json:"kind"`
	Num      float64 `json:"num"`
	Text     string  `json:"text"`
	Bits     uint32  `json:"bits"`
	ObjType  uint16  `json:"object_type"`
	Instance uint32  `json:"instance"`
}

type wantResult struct {
	ObjType  uint16      `json:"object_type"`
	Instance uint32      `json:"instance"`
	Property uint32      `json:"property"`
	Values   []wantValue `json:"values"`
	Error    *struct {
		Class uint32 `json:"class"`
		Code  uint32 `json:"code"`
	} `json:"error"`
}

type ackVector struct {
	Name  string `json:"name"`
	Frame string `json:"frame"`
	Want  struct {
		Kind     string       `json:"kind"`
		ObjType  uint16       `json:"object_type"`
		Instance uint32       `json:"instance"`
		Property uint32       `json:"property"`
		Values   []wantValue  `json:"values"`
		Results  []wantResult `json:"results"`
		Class    uint32       `json:"class"`
		Code     uint32       `json:"code"`
		Reason   uint32       `json:"reason"`
		MaxAPDU  uint32       `json:"max_apdu"`
		Vendor   uint32       `json:"vendor"`
		SrcNet   uint16       `json:"src_net"`
		SrcMAC   string       `json:"src_mac"`
	} `json:"want"`
}

func loadAcks(t *testing.T) []ackVector {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "acks.json"))
	if err != nil {
		t.Fatalf("read vectors: %v", err)
	}
	var doc struct {
		Vectors []ackVector `json:"vectors"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse vectors: %v", err)
	}
	if len(doc.Vectors) == 0 {
		t.Fatal("no vectors")
	}
	return doc.Vectors
}

func kindName(k byte) string {
	switch k {
	case tagNull:
		return "null"
	case tagBoolean:
		return "boolean"
	case tagUnsigned:
		return "unsigned"
	case tagSigned:
		return "signed"
	case tagReal:
		return "real"
	case tagDouble:
		return "double"
	case tagCharString:
		return "charstring"
	case tagBitString:
		return "bitstring"
	case tagEnumerated:
		return "enumerated"
	case tagObjectID:
		return "objectid"
	default:
		return fmt.Sprintf("tag-%d", k)
	}
}

func checkValues(t *testing.T, name string, got []Value, want []wantValue) {
	t.Helper()
	if len(got) != len(want) {
		t.Errorf("%s: %d values, want %d", name, len(got), len(want))
		return
	}
	for i := range want {
		g, w := got[i], want[i]
		if kindName(g.Kind) != w.Kind {
			t.Errorf("%s[%d]: kind %s, want %s", name, i, kindName(g.Kind), w.Kind)
			continue
		}
		switch w.Kind {
		case "real", "double", "unsigned", "enumerated", "signed", "boolean":
			if math.Abs(g.Num-w.Num) > 1e-4 {
				t.Errorf("%s[%d]: %v, want %v", name, i, g.Num, w.Num)
			}
		case "charstring":
			if g.Text != w.Text {
				t.Errorf("%s[%d]: %q, want %q", name, i, g.Text, w.Text)
			}
		case "bitstring":
			if g.Bits != w.Bits {
				t.Errorf("%s[%d]: bits %d, want %d", name, i, g.Bits, w.Bits)
			}
		case "objectid":
			if g.Object.Type != w.ObjType || g.Object.Instance != w.Instance {
				t.Errorf("%s[%d]: %v, want %d:%d", name, i, g.Object,
					w.ObjType, w.Instance)
			}
		}
	}
}

func TestDecodeSimulatorFrames(t *testing.T) {
	for _, v := range loadAcks(t) {
		t.Run(v.Name, func(t *testing.T) {
			raw, err := hex.DecodeString(v.Frame)
			if err != nil {
				t.Fatalf("bad hex: %v", err)
			}
			info, err := unframe(raw)
			if err != nil {
				t.Fatalf("unframe: %v", err)
			}
			if v.Want.SrcNet != 0 {
				if info.SrcNet != v.Want.SrcNet {
					t.Errorf("src net %d, want %d", info.SrcNet, v.Want.SrcNet)
				}
				if hex.EncodeToString(info.SrcMAC) != v.Want.SrcMAC {
					t.Errorf("src mac %x, want %s", info.SrcMAC, v.Want.SrcMAC)
				}
			}
			a, err := parseAPDU(info.APDU)
			if err != nil {
				t.Fatalf("parse apdu: %v", err)
			}

			switch v.Want.Kind {
			case "read_property":
				obj, prop, vals, err := parseReadPropertyAck(a.Payload)
				if err != nil {
					t.Fatalf("parse ack: %v", err)
				}
				if obj.Type != v.Want.ObjType || obj.Instance != v.Want.Instance {
					t.Errorf("object %v, want %d:%d", obj, v.Want.ObjType, v.Want.Instance)
				}
				if prop != v.Want.Property {
					t.Errorf("property %d, want %d", prop, v.Want.Property)
				}
				checkValues(t, v.Name, vals, v.Want.Values)

			case "rpm":
				results, err := parseRPMAck(a.Payload)
				if err != nil {
					t.Fatalf("parse rpm: %v", err)
				}
				if len(results) != len(v.Want.Results) {
					t.Fatalf("%d results, want %d", len(results), len(v.Want.Results))
				}
				for i, w := range v.Want.Results {
					g := results[i]
					if g.Object.Type != w.ObjType || g.Object.Instance != w.Instance {
						t.Errorf("result %d: object %v, want %d:%d", i, g.Object,
							w.ObjType, w.Instance)
					}
					if g.Prop != w.Property {
						t.Errorf("result %d: property %d, want %d", i, g.Prop, w.Property)
					}
					if w.Error != nil {
						if g.Err == nil {
							t.Errorf("result %d: expected an error", i)
							continue
						}
						if g.Err.Class != w.Error.Class || g.Err.Code != w.Error.Code {
							t.Errorf("result %d: error %d/%d, want %d/%d", i,
								g.Err.Class, g.Err.Code, w.Error.Class, w.Error.Code)
						}
						continue
					}
					if g.Err != nil {
						t.Errorf("result %d: unexpected error %v", i, g.Err)
						continue
					}
					checkValues(t, fmt.Sprintf("%s result %d", v.Name, i),
						g.Values, w.Values)
				}

			case "error":
				e := errorFrom(a)
				if e.Class != v.Want.Class || e.Code != v.Want.Code {
					t.Errorf("error %d/%d, want %d/%d", e.Class, e.Code,
						v.Want.Class, v.Want.Code)
				}
				if !e.IsUnknownObject() {
					t.Error("class 1 code 31 must read as unknown-object")
				}

			case "reject", "abort":
				e := errorFrom(a)
				if e.Reason != v.Want.Reason {
					t.Errorf("reason %d, want %d", e.Reason, v.Want.Reason)
				}

			case "i_am":
				obj, maxAPDU, vendor, err := parseIAm(a.Payload)
				if err != nil {
					t.Fatalf("parse i-am: %v", err)
				}
				if obj.Instance != v.Want.Instance {
					t.Errorf("instance %d, want %d", obj.Instance, v.Want.Instance)
				}
				if maxAPDU != v.Want.MaxAPDU {
					t.Errorf("max apdu %d, want %d", maxAPDU, v.Want.MaxAPDU)
				}
				if vendor != v.Want.Vendor {
					t.Errorf("vendor %d, want %d", vendor, v.Want.Vendor)
				}

			default:
				t.Fatalf("unhandled vector kind %q", v.Want.Kind)
			}
		})
	}
}

// ------------------------------------------------- request generation

type genRequest struct {
	Name  string `json:"name"`
	Frame string `json:"frame"`
}

// requestSet is the single source of truth for both halves of the interop
// check: the frames written for the device to parse, and the expectations
// asserted against what it understood.
func requestSet() []genRequest {
	dest := Address{IP: "10.52.11.20"}
	mstp := Address{IP: "10.52.11.20", Net: 2001, MAC: []byte{12}}
	return []genRequest{
		{"read_present_value", hex.EncodeToString(frame(
			readPropertyRequest(1, ObjectID{ObjAnalogInput, 4}, PropPresentValue),
			dest, true, false))},
		{"read_object_name", hex.EncodeToString(frame(
			readPropertyRequest(2, ObjectID{ObjAnalogInput, 4}, PropObjectName),
			dest, true, false))},
		{"read_object_list_count", hex.EncodeToString(frame(
			readPropertyIndexRequest(3, ObjectID{ObjDevice, 40001}, PropObjectList, 0),
			dest, true, false))},
		{"read_object_list_element", hex.EncodeToString(frame(
			readPropertyIndexRequest(4, ObjectID{ObjDevice, 40001}, PropObjectList, 7),
			dest, true, false))},
		{"rpm_three_objects", hex.EncodeToString(frame(
			readPropertyMultipleRequest(5, []ReadSpec{
				{ObjectID{ObjAnalogInput, 1}, []uint32{PropPresentValue, PropObjectName}},
				{ObjectID{ObjAnalogInput, 2}, []uint32{PropPresentValue}},
				{ObjectID{ObjBinaryInput, 3}, []uint32{PropPresentValue, PropStatusFlags}},
			}), dest, true, false))},
		{"who_is_global", hex.EncodeToString(frame(
			whoIsRequest(0, 0, false), dest, false, true))},
		{"who_is_ranged", hex.EncodeToString(frame(
			whoIsRequest(40001, 40099, true), dest, false, true))},
		{"subscribe_cov", hex.EncodeToString(frame(
			subscribeCOVRequest(6, 77, ObjectID{ObjAnalogInput, 1}, false, 600),
			dest, true, false))},
		// Routed: the device is on an MS/TP trunk behind the router's IP.
		{"routed_read_present_value", hex.EncodeToString(frame(
			readPropertyRequest(7, ObjectID{ObjAnalogInput, 2}, PropPresentValue),
			mstp, true, false))},
	}
}

// TestGenerateRequestVectors writes the requests the Go encoder produces so
// the simulator's decoders can be run over them. It only writes when asked:
//
//	DCIM_GEN_VECTORS=1 go test ./internal/adapters/bacnet/ -run Generate
func TestGenerateRequestVectors(t *testing.T) {
	if os.Getenv("DCIM_GEN_VECTORS") != "1" {
		t.Skip("set DCIM_GEN_VECTORS=1 to regenerate")
	}
	out := map[string]any{"requests": requestSet()}
	raw, err := json.MarshalIndent(out, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll("testdata", 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join("testdata", "requests.json"),
		append(raw, '\n'), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Logf("wrote %d requests; now run contracts/tools/gen_bacnet_vectors.py",
		len(requestSet()))
}

type decodedRequest struct {
	Name      string `json:"name"`
	Type      string `json:"type"`
	Service   int    `json:"service"`
	InvokeID  int    `json:"invoke_id"`
	ObjType   uint16 `json:"object_type"`
	Instance  uint32 `json:"instance"`
	Property  uint32 `json:"property"`
	ArrayIdx  int    `json:"array_index"`
	Low       int    `json:"low"`
	High      int    `json:"high"`
	ProcessID uint32 `json:"process_id"`
	Confirmed bool   `json:"confirmed"`
	Lifetime  uint32 `json:"lifetime"`
	Items     []struct {
		ObjType    uint16   `json:"object_type"`
		Instance   uint32   `json:"instance"`
		Properties []uint32 `json:"properties"`
	} `json:"items"`
}

// TestDeviceUnderstandsOurRequests closes the interop loop: the simulator's
// own request decoders are run over the frames this encoder produces, and this
// asserts the device read back what we meant to ask for.
//
// The failure this catches is the expensive one. A request that is subtly
// malformed does not error - the device answers a DIFFERENT object, or drops
// the array index and returns the whole list, and the data looks plausible.
func TestDeviceUnderstandsOurRequests(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "requests_decoded.json"))
	if err != nil {
		t.Skipf("no decoded requests yet (%v); run gen_bacnet_vectors.py", err)
	}
	var doc struct {
		Requests []decodedRequest `json:"requests"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse: %v", err)
	}
	byName := map[string]decodedRequest{}
	for _, r := range doc.Requests {
		byName[r.Name] = r
	}

	// Every request in the set must have been decoded; a silently missing one
	// would make this test pass by not checking anything.
	for _, r := range requestSet() {
		if _, ok := byName[r.Name]; !ok {
			t.Fatalf("%s was not decoded - regenerate the vectors", r.Name)
		}
	}

	check := func(name string, fn func(t *testing.T, d decodedRequest)) {
		t.Run(name, func(t *testing.T) { fn(t, byName[name]) })
	}

	check("read_present_value", func(t *testing.T, d decodedRequest) {
		if d.Type != "confirmed" || d.Service != svcReadProperty {
			t.Fatalf("type %s service %d", d.Type, d.Service)
		}
		if d.InvokeID != 1 {
			t.Errorf("invoke id %d, want 1", d.InvokeID)
		}
		if d.ObjType != ObjAnalogInput || d.Instance != 4 || d.Property != PropPresentValue {
			t.Errorf("device read %d:%d prop %d", d.ObjType, d.Instance, d.Property)
		}
		if d.ArrayIdx != -1 {
			t.Errorf("array index %d, want none", d.ArrayIdx)
		}
	})

	check("read_object_list_count", func(t *testing.T, d decodedRequest) {
		// Index 0 is the array LENGTH. If the index is lost, the device
		// returns the whole object list instead and the reply is a different
		// size and shape entirely.
		if d.ArrayIdx != 0 {
			t.Fatalf("array index %d, want 0", d.ArrayIdx)
		}
		if d.Property != PropObjectList {
			t.Errorf("property %d, want object-list", d.Property)
		}
	})

	check("read_object_list_element", func(t *testing.T, d decodedRequest) {
		if d.ArrayIdx != 7 {
			t.Fatalf("array index %d, want 7", d.ArrayIdx)
		}
	})

	check("rpm_three_objects", func(t *testing.T, d decodedRequest) {
		if d.Service != svcReadPropertyMultiple {
			t.Fatalf("service %d", d.Service)
		}
		if len(d.Items) != 3 {
			t.Fatalf("%d objects, want 3", len(d.Items))
		}
		if d.Items[0].ObjType != ObjAnalogInput || d.Items[0].Instance != 1 {
			t.Errorf("first object %d:%d", d.Items[0].ObjType, d.Items[0].Instance)
		}
		if len(d.Items[0].Properties) != 2 ||
			d.Items[0].Properties[0] != PropPresentValue ||
			d.Items[0].Properties[1] != PropObjectName {
			t.Errorf("first object properties %v", d.Items[0].Properties)
		}
		if d.Items[2].ObjType != ObjBinaryInput || len(d.Items[2].Properties) != 2 {
			t.Errorf("third object %d:%d props %v", d.Items[2].ObjType,
				d.Items[2].Instance, d.Items[2].Properties)
		}
	})

	check("who_is_global", func(t *testing.T, d decodedRequest) {
		if d.Type != "unconfirmed" || d.Service != svcWhoIs {
			t.Fatalf("type %s service %d", d.Type, d.Service)
		}
		if d.Low != -1 || d.High != -1 {
			t.Errorf("range %d..%d, want unrestricted", d.Low, d.High)
		}
	})

	check("who_is_ranged", func(t *testing.T, d decodedRequest) {
		if d.Low != 40001 || d.High != 40099 {
			t.Fatalf("range %d..%d, want 40001..40099", d.Low, d.High)
		}
	})

	check("subscribe_cov", func(t *testing.T, d decodedRequest) {
		if d.ProcessID != 77 || d.Instance != 1 || d.Lifetime != 600 {
			t.Fatalf("process %d instance %d lifetime %d",
				d.ProcessID, d.Instance, d.Lifetime)
		}
		if d.Confirmed {
			t.Error("subscribed confirmed; unconfirmed was requested")
		}
	})

	check("routed_read_present_value", func(t *testing.T, d decodedRequest) {
		// The routed header must not disturb the APDU behind it.
		if d.ObjType != ObjAnalogInput || d.Instance != 2 {
			t.Fatalf("device read %d:%d", d.ObjType, d.Instance)
		}
	})
}

// ---------------------------------------------------------- unit tests

func TestObjectIDPacking(t *testing.T) {
	for _, id := range []ObjectID{
		{ObjAnalogInput, 1}, {ObjBinaryInput, 4194303}, {ObjDevice, 40001},
		{ObjMultiStateIn, 0},
	} {
		if got := unpackObjectID(id.packed()); got != id {
			t.Errorf("%v round-tripped to %v", id, got)
		}
	}
}

// An MS/TP device has no IP of its own: the router's IP carries the packet and
// the DNET/DADR inside the NPDU says which device on the trunk it is.
func TestRoutedFrameCarriesDestination(t *testing.T) {
	plain := frame([]byte{0x01, 0x02}, Address{IP: "10.0.0.1"}, true, false)
	routed := frame([]byte{0x01, 0x02}, Address{IP: "10.0.0.1", Net: 2001,
		MAC: []byte{9}}, true, false)
	if len(routed) <= len(plain) {
		t.Fatal("routed frame carries no network header")
	}
	if routed[5]&0x20 == 0 {
		t.Fatal("DNET flag not set")
	}
	if routed[6] != 0x07 || routed[7] != 0xD1 {
		t.Fatalf("dnet bytes %02x%02x, want 07d1", routed[6], routed[7])
	}
	if routed[8] != 1 || routed[9] != 9 {
		t.Fatalf("dadr %d/%d, want len 1 mac 9", routed[8], routed[9])
	}
	if routed[len(routed)-3] != 255 {
		t.Fatalf("hop count %d, want 255", routed[len(routed)-3])
	}
}

func TestUnframeRejectsNonBACnet(t *testing.T) {
	if _, err := unframe([]byte{0x00, 0x01, 0x02, 0x03}); err == nil {
		t.Fatal("accepted a non-BVLL datagram")
	}
	if _, err := unframe([]byte{0x81}); err == nil {
		t.Fatal("accepted a truncated datagram")
	}
	// A length field longer than the datagram is the classic malformed frame.
	if _, err := unframe([]byte{0x81, 0x0A, 0xFF, 0xFF, 0x01, 0x00}); err == nil {
		t.Fatal("accepted a frame whose length exceeds the datagram")
	}
}

// A segmented response must be reported, not partially decoded: half an object
// list is indistinguishable from a short one.
func TestSegmentedResponseIsRejected(t *testing.T) {
	if _, err := parseAPDU([]byte{(pduComplexAck << 4) | 0x08, 1, 12, 0x00}); err == nil {
		t.Fatal("segmented ComplexAck was accepted")
	}
}

func TestMinUintEncoding(t *testing.T) {
	cases := map[uint32]int{0: 1, 255: 1, 256: 2, 65535: 2, 65536: 3, 1 << 24: 4}
	for v, want := range cases {
		if got := len(minUint(v)); got != want {
			t.Errorf("%d encoded in %d bytes, want %d", v, got, want)
		}
	}
}
