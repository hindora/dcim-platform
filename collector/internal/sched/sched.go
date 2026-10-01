// Package sched schedules polls and runs them on a bounded worker pool.
//
// Not one goroutine per endpoint: at ten thousand endpoints that is ten
// thousand timers. A one-second time wheel with a deterministic per-endpoint
// phase spreads the load evenly and survives restarts without re-thundering.
package sched

import (
	"context"
	"hash/fnv"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/hari/dcim-platform/collector/internal/obs"
	"github.com/hari/dcim-platform/collector/pkg/models"
)

// Job is one endpoint's recurring poll.
type Job struct {
	Endpoint *models.Endpoint
	Interval time.Duration
	nextRun  time.Time
	running  bool
	// When dispatch queued it. Written before the channel send and read
	// after the receive, so the channel orders the two.
	enqueued time.Time
}

type Runner func(ctx context.Context, ep *models.Endpoint)

type Scheduler struct {
	mu   sync.Mutex
	jobs map[string]*Job

	queue   chan *Job
	run     Runner
	log     *slog.Logger
	mets    *obs.Metrics
	workers int

	// Per-protocol and per-host limits. Per HOST, not per endpoint: a gateway
	// fronting six field devices is one host, and hammering it with six
	// concurrent reads produces timeouts that look like six dead sensors.
	protoSem map[string]chan struct{}
	hostSem  map[string]chan struct{}
	hostCap  map[string]int
	semMu    sync.Mutex

	// Capacity counters (docs/26 Phase 5), per protocol, cumulative for the
	// life of the process. Created lazily and never removed, so a pointer
	// handed out once stays valid and the hot path needs only atomics.
	stats   map[string]*protoStats
	statsMu sync.Mutex

	wg sync.WaitGroup
}

// protoStats is one protocol's cumulative capacity counters.
type protoStats struct {
	dispatched  atomic.Uint64
	overrun     atomic.Uint64
	shed        atomic.Uint64
	completed   atomic.Uint64
	late        atomic.Uint64
	busyNs      atomic.Uint64
	semWaitNs   atomic.Uint64
	queueWaitNs atomic.Uint64
}

// LateAfter is how long a queued poll may wait for a worker before it counts
// as late - Zabbix's own "queue" line for delayed items. Under it is normal
// jitter from the one-second wheel; over it, the pool is not keeping up.
const LateAfter = 5 * time.Second

// ProtoCounters is one protocol's reading. Scheduled and Jobs describe the
// job set right now; everything else is cumulative since the process
// started, so two readings subtract into a window.
type ProtoCounters struct {
	Jobs int `json:"jobs"`
	// Polls per second the current job set asks for: the sum of 1/interval.
	Scheduled float64 `json:"scheduled_per_s"`
	// The protocol's concurrency limit; 0 when only the worker pool bounds it.
	Limit      int    `json:"limit"`
	Dispatched uint64 `json:"dispatched"`
	// Due again while the previous poll of the same endpoint was still
	// running: one slow device, or a pool too busy to start it on time.
	Overrun uint64 `json:"overrun"`
	// Dropped because the queue was full - capacity, unambiguously.
	Shed      uint64 `json:"shed"`
	Completed uint64 `json:"completed"`
	// Started more than LateAfter after it was queued.
	Late uint64 `json:"late"`
	// Worker time held, from dequeue to done, including waits for a
	// protocol or per-host slot: a worker blocked on a semaphore is a
	// worker nobody else can use.
	BusyNs uint64 `json:"busy_ns"`
	// The part of BusyNs spent waiting for a protocol or per-host slot. A
	// large share means the protocol limit, not the pool, is the ceiling.
	SemWaitNs   uint64 `json:"sem_wait_ns"`
	QueueWaitNs uint64 `json:"queue_wait_ns"`
}

// Snapshot is a whole scheduler's capacity reading at one instant.
type Snapshot struct {
	At        time.Time
	Workers   int
	Protocols map[string]ProtoCounters
}

type Options struct {
	Workers       int
	QueueSize     int
	ProtoLimits   map[string]int
	PerHostLimits map[string]int
}

func New(opts Options, run Runner, log *slog.Logger, mets *obs.Metrics) *Scheduler {
	if opts.Workers < 1 {
		opts.Workers = 16
	}
	if opts.QueueSize < opts.Workers {
		opts.QueueSize = opts.Workers * 8
	}
	s := &Scheduler{
		jobs:     make(map[string]*Job),
		queue:    make(chan *Job, opts.QueueSize),
		run:      run,
		log:      log,
		mets:     mets,
		protoSem: make(map[string]chan struct{}),
		hostSem:  make(map[string]chan struct{}),
		hostCap:  make(map[string]int),
		stats:    make(map[string]*protoStats),
	}
	for proto, limit := range opts.ProtoLimits {
		if limit > 0 {
			s.protoSem[proto] = make(chan struct{}, limit)
		}
	}
	for proto, limit := range opts.PerHostLimits {
		if limit > 0 {
			s.hostCap[proto] = limit
		}
	}
	s.workers = opts.Workers
	return s
}

func (s *Scheduler) Start(ctx context.Context) {
	for i := 0; i < s.workers; i++ {
		s.wg.Add(1)
		go s.worker(ctx)
	}
	s.wg.Add(1)
	go s.tick(ctx)
}

func (s *Scheduler) Wait() { s.wg.Wait() }

// Add registers an endpoint. The first poll is placed at a deterministic phase
// within one interval so that 664 endpoints on a 30 s schedule fire ~22 per
// second instead of all at t=0.
func (s *Scheduler) Add(ep *models.Endpoint) {
	s.AddEvery(ep, ep.Poll.Interval())
}

// AddSoon registers an endpoint whose first poll lands within `within` rather
// than anywhere in its interval. For endpoints handed over at runtime - a
// failover, failback, drain or rebalance: the previous owner has stopped, so
// a first poll at the endpoint's spread slot left it unpolled for up to a
// whole interval. The live HA test measured the record moving at 3 min and
// polling recovering only at 4.7 min, for exactly this reason. Later polls
// keep the endpoint's interval; only the first is pulled in, still spread
// deterministically across `within` so a batch does not land in one tick.
func (s *Scheduler) AddSoon(ep *models.Endpoint, within time.Duration) {
	s.addAt(ep, ep.Poll.Interval(), within)
}

// AddEvery registers an endpoint on an interval other than its poll profile's.
// The availability scheduler uses it: liveness runs on its own cadence.
func (s *Scheduler) AddEvery(ep *models.Endpoint, interval time.Duration) {
	s.addAt(ep, interval, interval)
}

func (s *Scheduler) addAt(ep *models.Endpoint, interval, spread time.Duration) {
	if spread <= 0 || spread > interval {
		spread = interval
	}
	offset := phaseOffset(ep.ID, spread)

	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.jobs[ep.ID]; exists {
		s.jobs[ep.ID].Endpoint = ep
		s.jobs[ep.ID].Interval = interval
		return
	}
	s.jobs[ep.ID] = &Job{
		Endpoint: ep,
		Interval: interval,
		nextRun:  time.Now().Add(offset),
	}
}

func (s *Scheduler) Remove(endpointID string) {
	s.mu.Lock()
	delete(s.jobs, endpointID)
	s.mu.Unlock()
}

func (s *Scheduler) Count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.jobs)
}

func (s *Scheduler) tick(ctx context.Context) {
	defer s.wg.Done()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			s.dispatch(now)
		}
	}
}

func (s *Scheduler) dispatch(now time.Time) {
	s.mu.Lock()
	due := make([]*Job, 0, 32)
	for _, job := range s.jobs {
		if job.nextRun.After(now) {
			continue
		}
		job.nextRun = now.Add(job.Interval)
		if job.running {
			// Never queue the same endpoint twice: overlapping polls corrupt
			// counter deltas and produce impossible throughput spikes.
			s.mets.PollsSkipped.WithLabelValues(job.Endpoint.Protocol).Inc()
			s.statsFor(job.Endpoint.Protocol).overrun.Add(1)
			continue
		}
		job.running = true
		job.enqueued = now
		due = append(due, job)
	}
	s.mu.Unlock()

	for _, job := range due {
		st := s.statsFor(job.Endpoint.Protocol)
		select {
		case s.queue <- job:
			st.dispatched.Add(1)
		default:
			// Queue full: shed rather than block the wheel, and say so.
			s.mets.PollsShed.WithLabelValues(job.Endpoint.Protocol).Inc()
			st.shed.Add(1)
			s.markDone(job)
		}
	}
}

func (s *Scheduler) worker(ctx context.Context) {
	defer s.wg.Done()
	for {
		select {
		case <-ctx.Done():
			return
		case job := <-s.queue:
			s.execute(ctx, job)
		}
	}
}

func (s *Scheduler) execute(ctx context.Context, job *Job) {
	defer s.markDone(job)

	started := time.Now()
	st := s.statsFor(job.Endpoint.Protocol)
	if !job.enqueued.IsZero() {
		wait := started.Sub(job.enqueued)
		if wait > 0 {
			st.queueWaitNs.Add(uint64(wait))
		}
		if wait > LateAfter {
			st.late.Add(1)
		}
	}
	defer func() {
		st.busyNs.Add(uint64(time.Since(started)))
		st.completed.Add(1)
	}()

	// One malformed response must not take down collection for everything else.
	defer func() {
		if r := recover(); r != nil {
			s.log.Error("panic in poll", "endpoint_id", job.Endpoint.ID,
				"device", job.Endpoint.DeviceName, "panic", r)
		}
	}()

	proto := job.Endpoint.Protocol
	if sem := s.protoSem[proto]; sem != nil {
		select {
		case sem <- struct{}{}:
			defer func() { <-sem }()
		case <-ctx.Done():
			return
		}
	}
	if sem := s.hostSemFor(proto, job.Endpoint.Address); sem != nil {
		select {
		case sem <- struct{}{}:
			defer func() { <-sem }()
		case <-ctx.Done():
			return
		}
	}

	st.semWaitNs.Add(uint64(time.Since(started)))

	pollCtx, cancel := context.WithTimeout(ctx, job.Endpoint.Poll.Timeout()*
		time.Duration(job.Endpoint.Poll.Retries+1)+time.Second)
	defer cancel()
	s.run(pollCtx, job.Endpoint)
}

func (s *Scheduler) hostSemFor(proto, host string) chan struct{} {
	limit, ok := s.hostCap[proto]
	if !ok || limit <= 0 || host == "" {
		return nil
	}
	key := proto + "|" + host
	s.semMu.Lock()
	defer s.semMu.Unlock()
	sem, ok := s.hostSem[key]
	if !ok {
		sem = make(chan struct{}, limit)
		s.hostSem[key] = sem
	}
	return sem
}

func (s *Scheduler) statsFor(proto string) *protoStats {
	s.statsMu.Lock()
	defer s.statsMu.Unlock()
	st, ok := s.stats[proto]
	if !ok {
		st = &protoStats{}
		s.stats[proto] = st
	}
	return st
}

// Snapshot reads the capacity counters. Cheap enough for every heartbeat:
// one pass over the job set for the scheduled rate, atomics for the rest.
func (s *Scheduler) Snapshot() Snapshot {
	out := Snapshot{At: time.Now(), Workers: s.workers,
		Protocols: make(map[string]ProtoCounters)}
	s.mu.Lock()
	for _, job := range s.jobs {
		pc := out.Protocols[job.Endpoint.Protocol]
		pc.Jobs++
		if job.Interval > 0 {
			pc.Scheduled += 1 / job.Interval.Seconds()
		}
		out.Protocols[job.Endpoint.Protocol] = pc
	}
	s.mu.Unlock()

	s.statsMu.Lock()
	defer s.statsMu.Unlock()
	for proto, st := range s.stats {
		pc := out.Protocols[proto]
		pc.Dispatched = st.dispatched.Load()
		pc.Overrun = st.overrun.Load()
		pc.Shed = st.shed.Load()
		pc.Completed = st.completed.Load()
		pc.Late = st.late.Load()
		pc.BusyNs = st.busyNs.Load()
		pc.SemWaitNs = st.semWaitNs.Load()
		pc.QueueWaitNs = st.queueWaitNs.Load()
		out.Protocols[proto] = pc
	}
	for proto, pc := range out.Protocols {
		if sem := s.protoSem[proto]; sem != nil {
			pc.Limit = cap(sem)
			out.Protocols[proto] = pc
		}
	}
	return out
}

func (s *Scheduler) markDone(job *Job) {
	s.mu.Lock()
	job.running = false
	s.mu.Unlock()
}

// phaseOffset spreads endpoints deterministically across their interval, so a
// restart lands them in the same slots rather than re-thundering.
func phaseOffset(endpointID string, interval time.Duration) time.Duration {
	if interval <= 0 {
		return 0
	}
	h := fnv.New32a()
	_, _ = h.Write([]byte(endpointID))
	seconds := int64(interval / time.Second)
	if seconds <= 0 {
		return 0
	}
	return time.Duration(int64(h.Sum32())%seconds) * time.Second
}
