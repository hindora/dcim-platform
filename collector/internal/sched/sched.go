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

	wg sync.WaitGroup
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
	interval := ep.Poll.Interval()
	offset := phaseOffset(ep.ID, interval)

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
			continue
		}
		job.running = true
		due = append(due, job)
	}
	s.mu.Unlock()

	for _, job := range due {
		select {
		case s.queue <- job:
		default:
			// Queue full: shed rather than block the wheel, and say so.
			s.mets.PollsShed.WithLabelValues(job.Endpoint.Protocol).Inc()
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
