#!/usr/bin/env bash
# One-time host prep for Longhorn on Ubuntu. Run with sudo.
set -euo pipefail

# iSCSI: Longhorn attaches volumes over iSCSI
systemctl enable --now iscsid
modprobe iscsi_tcp
echo iscsi_tcp > /etc/modules-load.d/iscsi_tcp.conf

# multipathd grabs Longhorn's /dev/sd* devices and breaks mounts — blacklist them
if ! grep -q 'devnode "\^sd\[a-z0-9\]+"' /etc/multipath.conf 2>/dev/null; then
  cat >> /etc/multipath.conf <<'CONF'

blacklist {
    devnode "^sd[a-z0-9]+"
}
CONF
fi
systemctl restart multipathd

echo "iscsid: $(systemctl is-active iscsid)"
lsmod | grep -q iscsi_tcp && echo "iscsi_tcp: loaded"
