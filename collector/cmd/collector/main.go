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
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/hari/dcim-platform/collector/internal/app"
	"github.com/hari/dcim-platform/collector/internal/config"
	"github.com/hari/dcim-platform/collector/internal/mtls"
	"github.com/hari/dcim-platform/collector/internal/sealedbox"
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
	return 0
}
