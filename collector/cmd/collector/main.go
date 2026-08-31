// Command collector polls datacenter infrastructure and publishes canonical
// telemetry onto Redis Streams.
//
// It never touches the database: the ingest worker is the only writer. That
// separation is what lets the stream act as a buffer when the database is slow
// and what keeps the contract a message schema rather than a set of tables.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/hari/dcim-platform/collector/internal/app"
	"github.com/hari/dcim-platform/collector/internal/config"
)

var version = "0.1.0"

func main() {
	configPath := flag.String("config", "configs/collector.yaml", "path to the config file")
	showVersion := flag.Bool("version", false, "print the version and exit")
	flag.Parse()

	if *showVersion {
		fmt.Println(version)
		return
	}

	cfg, err := config.Load(*configPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "config error: %v\n", err)
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(),
		os.Interrupt, syscall.SIGTERM)
	defer stop()

	// The stored configuration is fetched BEFORE the adapters are built, so a
	// setting made in the UI is in force from the first poll rather than from
	// the second config fetch half a minute later.
	//
	// A failure here is not fatal. The file is a complete configuration on its
	// own, and a collector that cannot reach the API at boot still has an
	// estate to poll.
	boot := slog.New(slog.NewTextHandler(os.Stderr, nil))
	remote := config.NewRemoteClient(cfg, boot)
	if err := remote.Refresh(ctx); err != nil {
		boot.Warn("could not fetch stored configuration; running the file as-is",
			"error", err)
	} else {
		remote.Current().Apply(cfg)
	}

	application, err := app.New(cfg, version)
	if err != nil {
		fmt.Fprintf(os.Stderr, "startup error: %v\n", err)
		os.Exit(1)
	}
	application.SetConfigClient(remote)

	if err := application.Run(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "runtime error: %v\n", err)
		os.Exit(1)
	}
}
