#!/usr/bin/env bash
# One-time: configure the off-site target and install the hourly timer.
#   scripts/setup-offsite.sh <user>@<host> [remote path]
# Needs the target's password ONCE (to install the backup SSH key), then never again.
set -euo pipefail
cd "$(dirname "$0")/.."
target=${1:?usage: setup-offsite.sh user@host [remote-path]}
user=${target%@*}; host=${target#*@}; path=${2:-Backups/tiny-dgx/longhorn}
key=$HOME/.ssh/id_ed25519_backup

[[ -f $key ]] || ssh-keygen -q -t ed25519 -N "" -C "tiny-dgx-offsite-backup" -f "$key"
# Pin the host key now (trust on first use, while we know we're on the home LAN)
ssh-keygen -F "$host" >/dev/null || ssh-keyscan -T 5 -t ed25519 "$host" 2>/dev/null >> ~/.ssh/known_hosts
echo "Installing the backup key on $target (you'll be asked for its password once)..."
ssh-copy-id -i "$key.pub" "$target"
ssh -i "$key" -o BatchMode=yes "$target" "mkdir -p '$path'"

umask 077
printf 'OFFSITE_HOST=%s\nOFFSITE_USER=%s\nOFFSITE_PATH=%s\n' "$host" "$user" "$path" > secrets/offsite.env

mkdir -p ~/.config/systemd/user
ln -sf "$PWD/backup/systemd/homelab-offsite.service" ~/.config/systemd/user/
ln -sf "$PWD/backup/systemd/homelab-offsite.timer" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now homelab-offsite.timer
echo "First sync:"; backup/offsite-sync.sh --force
