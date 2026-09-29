#!/bin/sh
# Run by the .deb/.rpm before files are laid down. Creates the system user
# the systemd unit runs as - deploy/collector.service assumes it exists.
set -e

if ! getent group dcim-collector >/dev/null 2>&1; then
    groupadd --system dcim-collector
fi
if ! getent passwd dcim-collector >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --gid dcim-collector dcim-collector
fi

install -d -o dcim-collector -g dcim-collector -m 0750 /etc/dcim
