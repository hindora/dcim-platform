# Multi-stage: build with the full Go toolchain, ship a static binary on a
# distroless base. Nothing here needs a live checkout of contracts/mappings -
# the mapping bundle is embedded into the binary (see internal/mapping/
# embedded.go) - so this image is one self-contained artifact per release.
FROM golang:1.22-bookworm AS build

WORKDIR /src

# Dependencies first: they change less often than the source, so this layer
# stays cached across most rebuilds.
COPY collector/go.mod collector/go.sum ./
RUN go mod download

COPY collector/ .

ARG VERSION=dev
ARG GIT_SHA=unknown
# CGO_ENABLED=0 for a static binary: distroless/static carries no libc, and a
# CGO build would fail to even start on it. None of this collector's
# dependencies (gosnmp, go-redis, msgpack) need cgo.
RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags "-s -w -X main.version=${VERSION}-${GIT_SHA}" \
    -o /out/collector ./cmd/collector

# distroless/static: no shell, no package manager, nothing an attacker who
# reaches the process can pivot to. nonroot runs as uid 65532, which cannot
# bind the trap listener's privileged port 162 on its own - see
# deploy/collector-firewall.md for the two ways to grant that (a
# non-privileged listen port with a device-plane iptables redirect, or
# --cap-add=NET_BIND_SERVICE / Kubernetes' equivalent capability).
FROM gcr.io/distroless/static-debian12:nonroot

COPY --from=build /out/collector /usr/local/bin/collector
COPY collector/configs/collector.yaml /etc/dcim/collector.yaml

USER nonroot:nonroot

# Metrics and readiness. The trap listener (162/udp), Redfish event receiver
# (9143/tcp) and BACnet's local port are NOT exposed here - which ones are
# enabled, and on what port, is a config-time decision, and EXPOSE is
# documentation, not a firewall.
EXPOSE 9100 9101

ENTRYPOINT ["/usr/local/bin/collector", "--config", "/etc/dcim/collector.yaml"]
