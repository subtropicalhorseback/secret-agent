#!/bin/sh
# Hard-expiry fail-safe for the scoped home route (see home_route.py).
# Fires from chip-homeroute-expire.timer at the 900s cap, independently of the
# coordinator, so a crashed or SIGKILLed browse session cannot leave the box on
# the home route. Restoring full-VPN means DELETING state, so every step is
# idempotent and failure-tolerant: a no-op here is a safe outcome.
LOG="${CHIP_WORKSPACE:-/opt/chip}/data/logs/home_route.log"   # set CHIP_WORKSPACE in the unit

/usr/sbin/nft delete table inet chip_homeroute 2>/dev/null
/usr/sbin/nft delete table ip chip_homeroute_nat 2>/dev/null
rm -f /run/chip/home_route_lease /run/chip/rotate_paused

if /usr/sbin/nft list table inet chip_homeroute >/dev/null 2>&1; then
    restored="NO"
else
    restored="yes"
fi
printf '%s\tFORCED-EXPIRY\thard timer fired; scoped home route removed\trestored=%s\n' \
    "$(date -Is)" "$restored" >> "$LOG"
