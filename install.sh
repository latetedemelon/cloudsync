#!/bin/bash
# install.sh — install cloudsync binaries and scaffold /etc/cloudsync
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: run as root (sudo $0)"
    exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing binaries to /usr/local/bin"
install -m 0755 "$SRC_DIR/bin/cloudsync"               /usr/local/bin/cloudsync
install -m 0755 "$SRC_DIR/bin/cloudsync-realtime-tick" /usr/local/bin/cloudsync-realtime-tick

echo "==> Creating /etc/cloudsync"
mkdir -p /etc/cloudsync/secrets
chmod 0750 /etc/cloudsync
chmod 0700 /etc/cloudsync/secrets

if [ ! -f /etc/cloudsync/mappings.yaml ]; then
    echo "==> Installing example mappings.yaml"
    install -m 0640 "$SRC_DIR/examples/mappings.yaml" /etc/cloudsync/mappings.yaml
    echo "    Edit /etc/cloudsync/mappings.yaml before running setup-systemd."
else
    echo "==> /etc/cloudsync/mappings.yaml exists, leaving untouched"
fi

echo "==> Checking dependencies"
missing=()
for tool in rclone python3; do
    if ! command -v "$tool" >/dev/null; then
        missing+=("$tool")
    fi
done
# restic is only needed if you use backup mode; warn softly
if ! command -v restic >/dev/null; then
    echo "    note: restic not found (required only for 'backup' mode mappings)"
fi
# pyyaml
if ! python3 -c "import yaml" 2>/dev/null; then
    missing+=("python3-yaml")
fi

if [ "${#missing[@]}" -gt 0 ]; then
    echo
    echo "==> FAILED: required dependencies missing: ${missing[*]}"
    echo "    Install with: apt install ${missing[*]}  (or equivalent)"
    echo "    Then re-run this installer."
    exit 1
fi

cat <<'EOF'

==> Next steps:

  1. Configure one or more rclone remotes (S3, B2, GDrive, Dropbox,
     OneDrive, pCloud, SFTP, WebDAV, ... — any backend rclone supports):
       sudo rclone config --config /etc/cloudsync/rclone.conf
     See https://rclone.org/docs/#configure for per-backend setup.

  2. Verify each remote reaches its backend:
       sudo rclone --config /etc/cloudsync/rclone.conf listremotes
       sudo rclone --config /etc/cloudsync/rclone.conf lsd <remote-name>:

  3. For backup mappings, create password files:
       openssl rand -base64 32 | sudo tee /etc/cloudsync/secrets/<id>.pass
       sudo chmod 0400 /etc/cloudsync/secrets/<id>.pass

  4. Edit mappings:
       sudo $EDITOR /etc/cloudsync/mappings.yaml

  5. Validate:
       sudo cloudsync check

  6. Dry-run the systemd generation, then apply:
       sudo cloudsync setup-systemd --dry-run
       sudo cloudsync setup-systemd

  7. Inspect:
       systemctl list-timers 'cloudsync-*'
       journalctl -u 'cloudsync-*' -f

EOF
