#!/bin/sh
# Run by the .deb/.rpm after files are laid down.
set -e

systemctl daemon-reload || true

cat <<'EOF'

DCIM collector installed. It will not start yet:

  1. sudo cp /etc/dcim/collector.env.example /etc/dcim/collector.env
  2. Fill in DCIM_COLLECTOR_ID, DCIM_BASE_URL and DCIM_COLLECTOR_TOKEN
     (from Settings > Collectors > Add collector on the platform)
  3. sudo chmod 0640 /etc/dcim/collector.env && \
       sudo chown dcim-collector:dcim-collector /etc/dcim/collector.env
  4. sudo systemctl enable --now dcim-collector

See /usr/share/doc/dcim-collector/collector-firewall.md for the ports this
host needs open in each direction before step 4.
EOF
