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

	application, err := app.New(cfg, version)
	if err != nil {
		fmt.Fprintf(os.Stderr, "startup error: %v\n", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(),
		os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := application.Run(ctx); err != nil {
		fmt.Fprintf(os.Stderr, "runtime error: %v\n", err)
		os.Exit(1)
	}
}
