// Package snmp implements the SNMP poller.
//
// Two facts about this device plane drive the design and are worth stating
// where they cannot be missed:
//
//  1. The community string IS the agent's IP address, never "public". A wrong
//     community produces NO RESPONSE AT ALL, which is indistinguishable from a
//     dead device. If a whole class of devices looks offline, check the
//     community before anything else.
//
//  2. sysUpTime is read in the same cycle as the counters. A decrease means the
//     agent restarted and every counter reset, so the samples in that cycle
//     carry CounterReset and the ingest worker discards the delta instead of
//     publishing a four-billion-byte spike.
package snmp

import (
	"context"
	"fmt"
	"log/slog"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"

	g "github.com/gosnmp/gosnmp"

	"github.com/hari/dcim-platform/collector/internal/mapping"
	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

const sysUpTimeOID = "1.3.6.1.2.1.1.3.0"

type Adapter struct {
	maps           *mapping.Registry
	log            *slog.Logger
	mets           *obs.Metrics
	maxRepetitions int

	mu        sync.Mutex
	lastUptime map[string]float64 // endpoint id -> last sysUpTime in seconds
}

func New(maps *mapping.Registry, log *slog.Logger, mets *obs.Metrics,
	maxRepetitions int) *Adapter {
	if maxRepetitions <= 0 {
		maxRepetitions = 25
	}
	return &Adapter{
		maps: maps, log: log, mets: mets,
		maxRepetitions: maxRepetitions,
		lastUptime:     make(map[string]float64),
	}
}

func (a *Adapter) Protocol() string                   { return "snmp" }
func (a *Adapter) Init(_ context.Context) error       { return nil }
func (a *Adapter) Close(_ context.Context) error      { return nil }

func (a *Adapter) Forget(endpointID string) {
	a.mu.Lock()
	delete(a.lastUptime, endpointID)
	a.mu.Unlock()
}

func (a *Adapter) Poll(ctx context.Context, ep *models.Endpoint) (*models.PollOutcome, error) {
	community := ep.Credential.Community()
	if community == "" {
		// Fail loudly: with a wildcard-listener agent plane, an empty community
		// is not "use the default", it is a guaranteed silent drop.
		return nil, fmt.Errorf("%w: no community for endpoint %s",
			models.ErrAuth, ep.ID)
	}

	port := ep.Port
	if port == 0 {
		port = 161
	}

	client := &g.GoSNMP{
		Target:             ep.Address,
		Port:               uint16(port),
		Community:          community,
		Version:            g.Version2c,
		Timeout:            ep.Poll.Timeout(),
		Retries:            ep.Poll.Retries,
		MaxRepetitions:     uint32(a.maxRepetitions),
		ExponentialTimeout: false,
		Context:            ctx,
	}

	started := time.Now()
	if err := client.Connect(); err != nil {
		return nil, fmt.Errorf("%w: %v", models.ErrUnreachable, err)
	}
	defer client.Conn.Close()

	outcome := &models.PollOutcome{}
	now := models.NowMicros()

	// sysUpTime first, and in this same cycle, so counter resets are detected
	// before any counter is emitted.
	counterReset := a.checkUptime(ep, client, outcome, now)

	profiles := ep.Poll.MetricGroups
	if len(profiles) == 0 {
		profiles = []string{"system"}
	}
	for _, name := range profiles {
		profile, ok := a.maps.Profile(name)
		if !ok {
			a.log.Warn("unknown mapping profile", "profile", name,
				"endpoint_id", ep.ID)
			continue
		}
		a.collectScalars(client, profile, ep, outcome, now, counterReset)
		a.collectTables(client, profile, ep, outcome, now, counterReset)
	}

	outcome.LatencyMs = int(time.Since(started).Milliseconds())
	outcome.Partial = len(outcome.Misses) > 0

	if len(outcome.Samples) == 0 {
		// Reachable but silent is a real and distinct fault; do not report it
		// as a success with nothing to show.
		return outcome, fmt.Errorf("%w: no metrics returned", models.ErrDecode)
	}
	a.mets.SamplesTotal.WithLabelValues("snmp").Add(float64(len(outcome.Samples)))
	return outcome, nil
}

// checkUptime returns true when the agent appears to have restarted.
func (a *Adapter) checkUptime(ep *models.Endpoint, client *g.GoSNMP,
	outcome *models.PollOutcome, now int64) bool {

	result, err := client.Get([]string{sysUpTimeOID})
	if err != nil || len(result.Variables) == 0 {
		outcome.Misses = append(outcome.Misses,
			models.Miss{Metric: "sys_uptime", Reason: models.MissTimeout})
		return false
	}
	pdu := result.Variables[0]
	if pdu.Type == g.NoSuchObject || pdu.Type == g.NoSuchInstance {
		outcome.Misses = append(outcome.Misses,
			models.Miss{Metric: "sys_uptime", Reason: models.MissNoSuchObject})
		return false
	}

	ticks, ok := toFloat(pdu.Value)
	if !ok {
		return false
	}
	seconds := ticks / 100.0 // TimeTicks are centiseconds

	a.mu.Lock()
	previous, seen := a.lastUptime[ep.ID]
	a.lastUptime[ep.ID] = seconds
	a.mu.Unlock()

	outcome.Samples = append(outcome.Samples, models.Telemetry{
		EndpointID:     ep.ID,
		DeviceID:       ep.DeviceID,
		Metric:         models.MetricSysUptime,
		ValueType:      models.ValueTypeCounter,
		UintValue:      uint64(seconds),
		Unit:           "s",
		ObservedAt:     now,
		CollectedAt:    now,
		SourceProtocol: models.ProtocolSNMP,
		Quality:        models.QualityGood,
		CounterBits:    32,
		Metadata:       map[string]string{"oid": sysUpTimeOID},
	})

	return seen && seconds < previous
}

func (a *Adapter) collectScalars(client *g.GoSNMP, profile *mapping.Profile,
	ep *models.Endpoint, outcome *models.PollOutcome, now int64, reset bool) {

	if len(profile.Scalars) == 0 {
		return
	}
	oids := make([]string, 0, len(profile.Scalars))
	for _, s := range profile.Scalars {
		if s.OID == sysUpTimeOID {
			continue // already read, and re-reading would double-emit
		}
		oids = append(oids, s.OID)
	}
	if len(oids) == 0 {
		return
	}

	result, err := client.Get(oids)
	if err != nil {
		for _, s := range profile.Scalars {
			outcome.Misses = append(outcome.Misses,
				models.Miss{Metric: s.Metric, Reason: models.MissTimeout})
		}
		a.mets.MissesTotal.WithLabelValues("snmp", models.MissTimeout).
			Add(float64(len(profile.Scalars)))
		return
	}

	byOID := map[string]g.SnmpPDU{}
	for _, pdu := range result.Variables {
		byOID[strings.TrimPrefix(pdu.Name, ".")] = pdu
	}
	for _, s := range profile.Scalars {
		pdu, ok := byOID[s.OID]
		if !ok || pdu.Type == g.NoSuchObject || pdu.Type == g.NoSuchInstance {
			// A device that legitimately lacks an OID must not raise a data gap.
			outcome.Misses = append(outcome.Misses,
				models.Miss{Metric: s.Metric, Reason: models.MissNoSuchObject})
			a.mets.MissesTotal.WithLabelValues("snmp", models.MissNoSuchObject).Inc()
			continue
		}
		if sample, ok := a.sample(ep, s.Metric, s.ValueType, s.CounterBits, "",
			pdu.Value, s.Transform, now, reset, s.OID); ok {
			outcome.Samples = append(outcome.Samples, sample)
		}
	}
}

func (a *Adapter) collectTables(client *g.GoSNMP, profile *mapping.Profile,
	ep *models.Endpoint, outcome *models.PollOutcome, now int64, reset bool) {

	for ti := range profile.Tables {
		table := &profile.Tables[ti]

		// One walk per column, not per row. Collect every column the table
		// needs, including the ones used only for filtering and scaling.
		wanted := map[string]bool{}
		for _, c := range table.Columns {
			wanted[c.OID] = true
			if c.ScaleByColumn != "" {
				wanted[c.ScaleByColumn] = true
			}
			if c.PrecisionFrom != "" {
				wanted[c.PrecisionFrom] = true
			}
		}
		for _, d := range table.Derived {
			wanted[d.Numerator] = true
			wanted[d.Denominator] = true
		}
		if table.RowFilter != nil {
			wanted[table.RowFilter.OID] = true
		}
		if table.InstanceFrom != "" {
			wanted[table.InstanceFrom] = true
		}

		// rows[index][columnOID] = value
		rows := map[string]map[string]any{}
		for oid := range wanted {
			pdus, err := client.BulkWalkAll(oid)
			if err != nil {
				a.mets.MissesTotal.WithLabelValues("snmp", models.MissTimeout).Inc()
				continue
			}
			for _, pdu := range pdus {
				name := strings.TrimPrefix(pdu.Name, ".")
				index := strings.TrimPrefix(strings.TrimPrefix(name, oid), ".")
				if index == "" {
					index = "0"
				}
				row, ok := rows[index]
				if !ok {
					row = map[string]any{}
					rows[index] = row
				}
				row[oid] = pdu.Value
			}
		}

		var aggregate []float64
		var aggregateMetric string
		var aggregateUnit string

		for index, row := range rows {
			if !rowMatches(table.RowFilter, row) {
				continue
			}
			instance := index
			if table.InstanceFrom != "" {
				if v, ok := row[table.InstanceFrom]; ok {
					instance = toString(v)
				}
			}

			for _, c := range table.Columns {
				raw, ok := row[c.OID]
				if !ok {
					continue
				}
				value, ok := toFloat(raw)
				if !ok && c.ValueType != "text" {
					continue
				}
				// HOST-RESOURCES size/used are in ALLOCATION UNITS, not bytes.
				// Missing this scaling is the classic mistake with that MIB.
				if c.ScaleByColumn != "" {
					if units, ok := toFloat(row[c.ScaleByColumn]); ok && units > 0 {
						value *= units
					}
				}
				// ENTITY-SENSOR-MIB values are scaled by entPhySensorPrecision.
				if c.PrecisionFrom != "" {
					if p, ok := toFloat(row[c.PrecisionFrom]); ok && p != 0 {
						value /= math.Pow(10, p)
					}
				}

				if table.Aggregate != "" {
					aggregate = append(aggregate, value)
					aggregateMetric = c.Metric
					if def, ok := models.ValidateMetric(c.Metric); ok {
						aggregateUnit = def.Unit
					}
					continue
				}

				if sample, ok := a.sample(ep, c.Metric, c.ValueType, c.CounterBits,
					instance, raw, c.Transform, now, reset, c.OID); ok {
					// sample() re-reads raw; apply the scaling corrections.
					if c.ScaleByColumn != "" || c.PrecisionFrom != "" {
						sample.DoubleValue = c.Transform.Apply(value)
						sample.UintValue = 0
						sample.ValueType = models.ValueTypeGauge
					}
					outcome.Samples = append(outcome.Samples, sample)
				}
			}

			for _, d := range table.Derived {
				num, okN := toFloat(row[d.Numerator])
				den, okD := toFloat(row[d.Denominator])
				if !okN || !okD || den == 0 {
					continue
				}
				value := d.Transform.Apply(num / den)
				def, ok := models.ValidateMetric(d.Metric)
				if !ok {
					continue
				}
				outcome.Samples = append(outcome.Samples, models.Telemetry{
					EndpointID:     ep.ID,
					DeviceID:       ep.DeviceID,
					Metric:         d.Metric,
					Instance:       instance,
					ValueType:      models.ValueTypeGauge,
					DoubleValue:    value,
					Unit:           def.Unit,
					ObservedAt:     now,
					CollectedAt:    now,
					SourceProtocol: models.ProtocolSNMP,
					Quality:        quality(def, value),
					Metadata: map[string]string{
						"oid": d.Numerator + "/" + d.Denominator},
				})
			}
		}

		if table.Aggregate != "" && len(aggregate) > 0 {
			value := reduce(table.Aggregate, aggregate)
			def, ok := models.ValidateMetric(aggregateMetric)
			if ok {
				outcome.Samples = append(outcome.Samples, models.Telemetry{
					EndpointID:     ep.ID,
					DeviceID:       ep.DeviceID,
					Metric:         aggregateMetric,
					ValueType:      models.ValueTypeGauge,
					DoubleValue:    value,
					Unit:           aggregateUnit,
					ObservedAt:     now,
					CollectedAt:    now,
					SourceProtocol: models.ProtocolSNMP,
					Quality:        quality(def, value),
					Metadata: map[string]string{
						"table": table.Name, "aggregate": table.Aggregate,
						"rows": strconv.Itoa(len(aggregate))},
				})
			}
		}
	}
}

// sample builds one canonical Telemetry from a raw PDU value.
func (a *Adapter) sample(ep *models.Endpoint, metric, valueType string, bits int,
	instance string, raw any, tr *mapping.Transform, now int64, reset bool,
	oid string) (models.Telemetry, bool) {

	def, ok := models.ValidateMetric(metric)
	if !ok {
		// Emitting an unregistered key would be dropped downstream anyway; a
		// warning here is how a contract-version mismatch gets noticed.
		a.log.Warn("mapping references an unknown metric", "metric", metric)
		return models.Telemetry{}, false
	}

	t := models.Telemetry{
		EndpointID:     ep.ID,
		DeviceID:       ep.DeviceID,
		Metric:         metric,
		Instance:       instance,
		Unit:           def.Unit,
		ObservedAt:     now,
		CollectedAt:    now,
		SourceProtocol: models.ProtocolSNMP,
		Quality:        models.QualityGood,
		Metadata:       map[string]string{"oid": oid},
	}

	switch valueType {
	case "counter":
		v, ok := toUint(raw)
		if !ok {
			return t, false
		}
		t.ValueType = models.ValueTypeCounter
		t.UintValue = v
		t.CounterReset = reset
		t.CounterBits = uint32(bits)
		if t.CounterBits == 0 {
			t.CounterBits = 64
		}
	case "bool":
		v, ok := toInt(raw)
		if !ok {
			return t, false
		}
		t.ValueType = models.ValueTypeBool
		t.BoolValue = tr.Bool(v)
	case "text":
		t.ValueType = models.ValueTypeText
		t.TextValue = toString(raw)
	default:
		v, ok := toFloat(raw)
		if !ok {
			return t, false
		}
		value := tr.Apply(v)
		t.ValueType = models.ValueTypeGauge
		t.DoubleValue = value
		t.Quality = quality(def, value)
	}
	return t, true
}

// quality marks a reading outside the registry's declared range as SUSPECT
// rather than dropping it: the value is evidence, and hiding it makes a sensor
// fault look like a data gap.
func quality(def models.MetricDef, v float64) models.Quality {
	if def.HasMin && v < def.MinValid {
		return models.QualitySuspect
	}
	if def.HasMax && v > def.MaxValid {
		return models.QualitySuspect
	}
	return models.QualityGood
}

func rowMatches(f *mapping.RowFilter, row map[string]any) bool {
	if f == nil {
		return true
	}
	raw, ok := row[f.OID]
	if !ok {
		return false
	}
	if f.EqualsInt != nil {
		v, ok := toInt(raw)
		return ok && v == *f.EqualsInt
	}
	if f.Equals != "" {
		return strings.TrimPrefix(toString(raw), ".") == strings.TrimPrefix(f.Equals, ".")
	}
	return true
}

func reduce(mode string, values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	switch mode {
	case "max":
		out := values[0]
		for _, v := range values[1:] {
			if v > out {
				out = v
			}
		}
		return out
	case "min":
		out := values[0]
		for _, v := range values[1:] {
			if v < out {
				out = v
			}
		}
		return out
	case "sum":
		var sum float64
		for _, v := range values {
			sum += v
		}
		return sum
	default: // avg
		var sum float64
		for _, v := range values {
			sum += v
		}
		return sum / float64(len(values))
	}
}

func toFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case int:
		return float64(x), true
	case int32:
		return float64(x), true
	case int64:
		return float64(x), true
	case uint:
		return float64(x), true
	case uint32:
		return float64(x), true
	case uint64:
		return float64(x), true
	case float32:
		return float64(x), true
	case float64:
		return x, true
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(x), 64)
		return f, err == nil
	case []byte:
		f, err := strconv.ParseFloat(strings.TrimSpace(string(x)), 64)
		return f, err == nil
	default:
		return 0, false
	}
}

func toUint(v any) (uint64, bool) {
	if f, ok := toFloat(v); ok && f >= 0 {
		return uint64(f), true
	}
	return 0, false
}

func toInt(v any) (int64, bool) {
	if f, ok := toFloat(v); ok {
		return int64(f), true
	}
	return 0, false
}

func toString(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case []byte:
		return string(x)
	default:
		return fmt.Sprintf("%v", x)
	}
}

var _ models.Adapter = (*Adapter)(nil)
