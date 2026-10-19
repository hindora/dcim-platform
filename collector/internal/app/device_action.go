package app

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"time"

	"github.com/hari/dcim-platform/collector/pkg/models"
)

// A device action is the platform asking this collector to WRITE to a device it
// owns: switch a PDU outlet over SNMP, reset a server through its BMC. The
// collector is otherwise read-only, so the write path is narrow on purpose.
//
// The platform is trusted to ask, but this process is the one holding the
// credential and sitting on the management network, so it also checks what it
// is asked for. Only the control OIDs of the PDU families this platform speaks
// and the one Redfish action, with the values those define, get through; an
// arbitrary SET or POST named in a command - from a bug, or from a platform
// someone has taken over - is refused here, not obeyed.
type deviceAction struct {
	DeviceID    string         `json:"device_id"`
	DeviceName  string         `json:"device_name"`
	EndpointID  string         `json:"endpoint_id"`
	Op          string         `json:"op"`
	Label       string         `json:"label"`
	RequestedBy string         `json:"requested_by"`
	Reason      string         `json:"reason"`
	OID         string         `json:"oid"`
	Type        string         `json:"type"`
	Value       int            `json:"value"`
	ActionPath  string         `json:"action_path"`
	Body        map[string]any `json:"body"`
}

var (
	// rPDU2OutletSwitchedControlCommand.<outlet>
	apcOutletControl = regexp.MustCompile(`^1\.3\.6\.1\.4\.1\.318\.1\.1\.26\.9\.2\.4\.1\.5\.\d{1,3}$`)
	// outletSwitchingOperation.<pdu>.<outlet>
	raritanOutletOp = regexp.MustCompile(`^1\.3\.6\.1\.4\.1\.13742\.6\.4\.1\.2\.1\.2\.\d{1,3}\.\d{1,3}$`)

	allowedResets = map[string]bool{
		"On": true, "ForceOff": true, "GracefulShutdown": true, "GracefulRestart": true,
		"ForceRestart": true, "PowerCycle": true,
	}
)

const resetActionPath = "Actions/ComputerSystem.Reset"

// validateAction refuses anything outside the narrow set a device action may be.
func validateAction(act *deviceAction, ep *models.Endpoint) error {
	if ep == nil {
		return fmt.Errorf("this collector does not own that endpoint (any more)")
	}
	switch act.Op {
	case "snmp_set":
		if ep.Protocol != "snmp" {
			return fmt.Errorf("an SNMP write was aimed at a %s endpoint", ep.Protocol)
		}
		if ep.WriteCredential == nil {
			return fmt.Errorf("this endpoint has no write credential: refused")
		}
		if act.Type != "integer" {
			return fmt.Errorf("only INTEGER writes are allowed, not %q", act.Type)
		}
		if !apcOutletControl.MatchString(act.OID) && !raritanOutletOp.MatchString(act.OID) {
			return fmt.Errorf("%s is not a PDU outlet-control OID: refused", act.OID)
		}
		if act.Value < 0 || act.Value > 3 {
			return fmt.Errorf("%d is not an outlet command: refused", act.Value)
		}
	case "redfish_action":
		if ep.Protocol != "redfish" {
			return fmt.Errorf("a Redfish action was aimed at a %s endpoint", ep.Protocol)
		}
		if act.ActionPath != resetActionPath {
			return fmt.Errorf("%q is not an allowed Redfish action: refused", act.ActionPath)
		}
		rt, _ := act.Body["ResetType"].(string)
		if len(act.Body) != 1 || !allowedResets[rt] {
			return fmt.Errorf("%v is not an allowed reset: refused", act.Body)
		}
	default:
		return fmt.Errorf("unknown device action %q", act.Op)
	}
	return nil
}

// setPortOf is where this endpoint takes writes: its addressing.set_port when
// the agent serves them elsewhere than it is polled, else zero (the polled
// port). JSON numbers arrive as float64; an operator may also have typed text.
func setPortOf(ep *models.Endpoint) int {
	switch v := ep.Addressing["set_port"].(type) {
	case float64:
		return int(v)
	case int:
		return v
	case string:
		n, _ := strconv.Atoi(v)
		return n
	}
	return 0
}

func (a *App) endpointByID(id string) *models.Endpoint {
	if a.assign == nil {
		return nil
	}
	for _, ep := range a.assign.Endpoints() {
		if ep.ID == id {
			return ep
		}
	}
	return nil
}

// handleDeviceAction carries out one queued action and reports how it went. It
// runs on its own goroutine so a slow BMC cannot hold up the command poll, and
// always reports: a platform waiting on a button must hear back.
func (a *App) handleDeviceAction(parent context.Context, base, id string, raw json.RawMessage) {
	// Reports go out on the PARENT context: the action's own deadline has
	// usually passed by the time a timeout is being reported.
	ctx := parent
	var act deviceAction
	if err := json.Unmarshal(raw, &act); err != nil {
		a.reportCommand(parent, base, id, "failed", "the command could not be read: "+err.Error(), "")
		return
	}
	ep := a.endpointByID(act.EndpointID)
	if err := validateAction(&act, ep); err != nil {
		a.log.Warn("device action refused", "command_id", id, "device", act.DeviceName,
			"op", act.Op, "reason", err.Error())
		a.reportCommand(parent, base, id, "failed", err.Error(), "")
		return
	}

	ctx, cancel := context.WithTimeout(ctx, 25*time.Second)
	defer cancel()
	a.log.Info("device action starting", "command_id", id, "device", act.DeviceName,
		"op", act.Op, "label", act.Label, "by", act.RequestedBy, "reason", act.Reason)

	var err error
	switch act.Op {
	case "snmp_set":
		if a.snmp == nil {
			err = fmt.Errorf("this collector has no SNMP adapter")
		} else {
			err = a.snmp.SetInteger(ctx, ep, setPortOf(ep), act.OID, act.Value)
		}
	case "redfish_action":
		if a.redfish == nil {
			err = fmt.Errorf("this collector has no Redfish adapter")
		} else {
			err = a.redfish.Action(ctx, ep, act.ActionPath, act.Body)
		}
	}
	if err != nil {
		a.reportCommand(parent, base, id, "failed", fmt.Sprintf("%s on %s: %v", act.Label, act.DeviceName, err), "")
		return
	}
	a.reportCommand(parent, base, id, "succeeded", fmt.Sprintf("%s on %s", act.Label, act.DeviceName), "")
}
