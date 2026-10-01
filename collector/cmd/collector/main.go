// Command collector polls datacenter infrastructure and publishes canonical
// telemetry onto Redis Streams.
//
// It never touches the database: the ingest worker is the only writer. That
// separation is what lets the stream act as a buffer when the database is slow
// and what keeps the contract a message schema rather than a set of tables.
package main

import (
	"context"
	"encoding/base64"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/hari/dcim-platform/collector/internal/app"
	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/mtls"
	"github.com/hari/dcim-platform/collector/internal/preflight"
	"github.com/hari/dcim-platform/collector/internal/sealedbox"
	"github.com/hari/dcim-platform/collector/internal/update"
)

// Overridden at build time via -ldflags "-X main.version=...". "dev" is what
// a plain `go build` or `go run` produces - accurate for a developer's own
// binary, and visibly not a release when it turns up in a heartbeat or an
// alarm's collector_id context.
var version = "dev"

func main() {
	// A subcommand, not a flag: `dcim-collector enroll ...` runs once, writes
	// a certificate to disk, and exits. It shares no state with the
	// long-running process below and must be parsed before flag.Parse()
	// touches os.Args at all.
	if len(os.Args) > 1 && os.Args[1] == "enroll" {
		os.Exit(runEnroll(os.Args[2:]))
	}
	if len(os.Args) > 1 && os.Args[1] == "upgrade-watch" {
		os.Exit(runUpgradeWatch(os.Args[2:]))
	}
	if len(os.Args) > 1 && os.Args[1] == "preflight" {
		os.Exit(runPreflight(os.Args[2:]))
	}

	configPath := flag.String("config", "configs/collector.yaml", "path to the config file")
	collectorID := flag.String("id", "",
		"collector id, overriding the file and DCIM_COLLECTOR_ID")
	showVersion := flag.Bool("version", false, "print the version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Println(version)
		return
	}

	cfg, err := config.Load(*configPath)
	if err == nil && *collectorID != "" {
		cfg.Collector.ID = *collectorID
		err = cfg.Validate()
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "config error: %v\n", err)
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(),
		os.Interrupt, syscall.SIGTERM)
	defer stop()

	// A certificate on disk from a previous `enroll` - loaded before anything
	// else touches the network, so every request this process makes,
	// starting with the config fetch two lines down, presents it. Not being
	// enrolled is not an error: cert is nil and every http.Client built from
	// store.TLSConfig() simply presents none, which is the bearer-token path
	// unchanged.
	boot := slog.New(slog.NewTextHandler(os.Stderr, nil))
	store := mtls.NewStore()
	if cert, notAfter, err := mtls.Load(cfg.Collector.StateDir); err != nil {
		boot.Warn("could not load an enrolled certificate; using the bearer "+
			"token instead", "error", err)
	} else if cert != nil {
		store.Set(*cert, notAfter)
		boot.Info("loaded enrolled certificate", "not_after", notAfter)
	}

	// The stored configuration is fetched BEFORE the adapters are built, so a
	// setting made in the UI is in force from the first poll rather than from
	// the second config fetch half a minute later.
	//
	// A failure here is not fatal. The file is a complete configuration on its
	// own, and a collector that cannot reach the API at boot still has an
	// estate to poll.
	remote := config.NewRemoteClient(cfg, boot, store.TLSConfig())
	if err := remote.Refresh(ctx); err != nil {
		boot.Warn("could not fetch stored configuration; running the file as-is",
			"error", err)
	} else {
		remote.Current().Apply(cfg)
	}

	application, err := app.New(cfg, version, store)
	if err != nil {
		fmt.Fprintf(os.Stderr, "startup error: %v\n", err)
		os.Exit(1)
	}
	application.SetConfigClient(remote)

	if store.Enrolled() {
		go mtls.RenewLoop(ctx, cfg.DCIM.BaseURL, cfg.Collector.ID, cfg.Collector.StateDir,
			30*24*time.Hour, time.Hour, store, boot)
	}

	if err := application.Run(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "runtime error: %v\n", err)
		os.Exit(1)
	}
}

func runEnroll(args []string) int {
	fs := flag.NewFlagSet("enroll", flag.ExitOnError)
	server := fs.String("server", "", "base URL of the DCIM platform, e.g. https://dcim.example.com")
	id := fs.String("id", "", "the collector id this token was issued for")
	token := fs.String("token", "", "the one-time enrollment token")
	stateDir := fs.String("state-dir", "./state",
		"where to write the certificate and key (0600, non-world-readable)")
	timeout := fs.Duration("timeout", 15*time.Second, "request timeout")
	configPath := fs.String("config", "configs/collector.yaml",
		"config file to run preflight against afterward - missing or invalid is not "+
			"fatal to enrollment itself, only to the automatic preflight run")
	skipPreflight := fs.Bool("skip-preflight", false,
		"do not run preflight automatically after a successful enroll")
	_ = fs.Parse(args)

	if *server == "" || *id == "" || *token == "" {
		fmt.Fprintln(os.Stderr, "usage: dcim-collector enroll --server <url> --id <collector-id> --token <token>")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// The credential-sealing keypair (docs/26 Phase 4) is separate from the
	// mTLS identity Enroll below negotiates, and long-lived across
	// certificate renewals - LoadOrGenerate so re-running enroll against an
	// already-provisioned state dir does not silently orphan whatever the
	// platform has on file for this collector.
	sealKP, err := sealedbox.LoadOrGenerate(*stateDir)
	if err != nil {
		fmt.Fprintf(os.Stderr, "could not prepare credential-sealing key: %v\n", err)
		return 1
	}
	sealPub := base64.StdEncoding.EncodeToString(sealKP.PublicKeyBytes())

	if err := mtls.Enroll(ctx, *server, *id, *token, *stateDir, sealPub, *timeout); err != nil {
		fmt.Fprintf(os.Stderr, "enrollment failed: %v\n", err)
		return 1
	}
	fmt.Printf("enrolled %s - certificate and key written to %s\n", *id, *stateDir)
	fmt.Println("start the collector normally; it loads the certificate from " +
		"the same state directory automatically.")

	if *skipPreflight {
		return 0
	}
	// docs/26 Phase 8: preflight runs automatically here, using the
	// certificate Enroll just wrote - an install that never gets this far
	// on its own is exactly what the onboarding wizard's preflight step
	// exists to catch immediately, not thirty seconds later on the first
	// real assignment fetch. A config file that cannot be read or does not
	// validate is NOT fatal to enroll's own exit code - only to which
	// checks preflight can meaningfully run - so config.Default() with the
	// id and state dir just used is the fallback, which still exercises
	// NTP and core reachability even with nothing else configured yet.
	cfg := config.Default()
	cfg.Collector.ID = *id
	cfg.Collector.StateDir = *stateDir
	cfg.DCIM.BaseURL = *server
	if loaded, loadErr := config.Load(*configPath); loadErr == nil {
		cfg = loaded
		cfg.Collector.ID = *id
	} else {
		fmt.Fprintf(os.Stderr, "preflight: could not read %s (%v); running with "+
			"defaults - some checks will show as skipped\n", *configPath, loadErr)
	}
	runPreflightWith(ctx, cfg, *stateDir, true)
	return 0
}

func runPreflight(args []string) int {
	fs := flag.NewFlagSet("preflight", flag.ExitOnError)
	configPath := fs.String("config", "configs/collector.yaml", "path to the config file")
	collectorID := fs.String("id", "",
		"collector id, overriding the file and DCIM_COLLECTOR_ID")
	post := fs.Bool("post", true, "post results to the platform")
	_ = fs.Parse(args)

	cfg, err := config.Load(*configPath)
	if err == nil && *collectorID != "" {
		cfg.Collector.ID = *collectorID
		err = cfg.Validate()
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "config error: %v\n", err)
		return 2
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return runPreflightWith(ctx, cfg, cfg.Collector.StateDir, *post)
}

// runPreflightWith is shared by the standalone `preflight` command and
// enroll's automatic run - the only difference between the two call sites
// is where the config came from and whether skip-preflight was passed.
func runPreflightWith(ctx context.Context, cfg *config.Config, stateDir string, post bool) int {
	store := mtls.NewStore()
	if cert, notAfter, err := mtls.Load(stateDir); err == nil && cert != nil {
		store.Set(*cert, notAfter)
	}
	client := &http.Client{Timeout: cfg.Preflight.Timeout,
		Transport: &http.Transport{TLSClientConfig: store.TLSConfig()}}

	checks := preflight.Run(ctx, cfg, client)
	failed := false
	for _, c := range checks {
		fmt.Printf("[%s] %-12s %s\n", c.Status, c.Check, c.Detail)
		if c.Status == preflight.StatusFail {
			failed = true
		}
	}

	if post {
		if err := preflight.Post(ctx, client, cfg.DCIM.BaseURL, cfg.Token(), checks); err != nil {
			fmt.Fprintf(os.Stderr, "preflight: could not post results to the platform: %v\n", err)
		} else {
			fmt.Println("preflight: results posted to the platform")
		}
	}

	if failed {
		return 1
	}
	return 0
}

// runUpgradeWatch is the rollback watcher (docs/26 Phase 7): the PREVIOUS
// build, started detached by the one being replaced. It never runs a poll.
func runUpgradeWatch(args []string) int {
	fs := flag.NewFlagSet("upgrade-watch", flag.ExitOnError)
	stateDir := fs.String("state-dir", "./state", "the collector's state directory")
	_ = fs.Parse(args)
	logf := func(format string, a ...any) {
		fmt.Println(time.Now().UTC().Format(time.RFC3339), "upgrade-watch:",
			fmt.Sprintf(format, a...))
	}
	logf("watching %s", update.MarkerPath(*stateDir))
	if err := update.Watch(*stateDir, 3*time.Second, update.KillMatching, update.Alive,
		update.StartDetached, logf); err != nil {
		logf("watcher error: %v", err)
		return 1
	}
	return 0
}
