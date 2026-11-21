# Scoped home route — system files

Reference copies of the out-of-tree files that `home_route.py` depends on.
**These are copies, not the live files.** Editing one here changes nothing
until it is installed. Live locations:

| Repo copy                       | Installed at                                      | Owner |
|---------------------------------|---------------------------------------------------|-------|
| `homeroute-on.nft`              | `/etc/chip/homeroute-on.nft`                      | root, 644 |
| `homeroute-expire.sh`           | `/etc/chip/homeroute-expire.sh`                   | root, 755 |
| `chip-homeroute-expire.service` | `/etc/systemd/system/chip-homeroute-expire.service` | root, 644 |
| `chip-homeroute-expire.timer`   | `/etc/systemd/system/chip-homeroute-expire.timer` | root, 644 |
| `sudoers.chip-homeroute`        | `/etc/sudoers.d/chip-homeroute`                   | root, 440 |

Without them `browse: ... via home` fails closed (the lease cannot be taken and
the browse returns "Couldn't open the scoped home route") — it never silently
falls back to routing the whole box off the VPN.

## Install / reinstall

```sh
sudo install -d -m 755 /etc/chip
sudo install -m 644 homeroute-on.nft              /etc/chip/homeroute-on.nft
sudo install -m 755 homeroute-expire.sh           /etc/chip/homeroute-expire.sh
sudo install -m 644 chip-homeroute-expire.service /etc/systemd/system/
sudo install -m 644 chip-homeroute-expire.timer   /etc/systemd/system/
sudo install -m 440 sudoers.chip-homeroute        /etc/sudoers.d/chip-homeroute
sudo visudo -c -f /etc/sudoers.d/chip-homeroute
sudo systemctl daemon-reload
```

Do **not** `systemctl enable` the timer: it is armed on demand by
`home_route.py acquire` and must not be running at boot.

## Why the sudoers file looks the way it does

Every entry is a fixed binary with fixed arguments and no wildcards, and the
only rule file is root-owned. The coordinator runs as its service user (`chip` in the template —
replace it with yours) non-interactively,
so it cannot answer a password prompt; these seven commands are the complete
set it needs. None of them can exec arbitrary code as root.

## Kernel dependency

The split tunnel matches the browser's cgroup with nftables
`socket cgroupv2`, which needs `CONFIG_NFT_SOCKET` (present as a module here).
The iptables equivalent (`xt_cgroup --path`) is **not** built on this Pi
kernel — that is why this is nftables and not iptables. Verify after a kernel
upgrade:

```sh
sudo nft -c -f /etc/chip/homeroute-on.nft && echo ok
```

Self-test the whole path (should print a residential IP, then a Mullvad one):

```sh
venv/bin/python3 home_route.py acquire --user selftest --task selftest --ttl 60
venv/bin/python3 home_route.py status
venv/bin/python3 home_route.py release --reason selftest
```
