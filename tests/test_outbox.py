"""Outbox (One Voice spec A.3): background scripts tell Chip, not the phone.

Standalone, no pytest, no signal-cli: direct sends are captured.
Exits non-zero on failure.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="outbox_test_")
os.environ["CHIP_OUTBOX_DIR"] = TMP

from skills import outbox, self_heal  # noqa: E402

DIRECT = []
outbox.direct_send = lambda text, recipient=None: DIRECT.append(text) or True
FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILS.append(f"{name}: {detail}")


def stale_heartbeat():
    outbox.beat()
    old = time.time() - 600
    os.utime(os.path.join(TMP, ".heartbeat"), (old, old))


print("routine event while Chip is consuming")
outbox.beat()
r = outbox.notify("lease", "Home-route lease released — full VPN restored.", urgent=False)
check("queued, not sent", r == "queued" and not DIRECT, (r, DIRECT))
urgent = outbox.drain()
check("drain returns nothing urgent", urgent == [], urgent)
check("it went to the digest", "lease" in open(os.path.join(TMP, "digest.jsonl")).read())

print("urgent events while Chip is consuming")
outbox.beat()
outbox.notify("vpn_down", "⚠️ VPN DOWN\nexit node unreachable", urgent=True)
outbox.notify("job_failure", "Scheduled job 'backup' has failed 3 times in a row.", urgent=True)
check("nothing sent directly", not DIRECT, DIRECT)
urgent = outbox.drain()
check("both come back from the drain", len(urgent) == 2, urgent)
msg = outbox.compose(urgent)
check("composed into ONE message", "VPN DOWN" in msg and "backup" in msg and msg.count("•") == 2, msg)
check("the outbox is empty after the drain",
      not [f for f in os.listdir(TMP) if f.endswith(".json")], os.listdir(TMP))

print("urgent event while Chip is down")
stale_heartbeat()
r = outbox.notify("vpn_down", "⚠️ VPN DOWN\nTailscale backend is Stopped", urgent=True)
check("sent directly when the heartbeat is stale", r == "sent-direct" and len(DIRECT) == 1, (r, DIRECT))
check("the direct message carries the [Chip] tag", DIRECT and DIRECT[0].startswith("[Chip] "), DIRECT)
check("nothing left queued to be sent twice",
      not [f for f in os.listdir(TMP) if f.endswith(".json")], os.listdir(TMP))

print("routine event while Chip is down")
DIRECT.clear()
stale_heartbeat()
r = outbox.notify("pip_report", "Monthly package check: 3 updates.", urgent=False)
check("routine events wait even when Chip is down", r == "queued" and not DIRECT, (r, DIRECT))

print("nightly digest")
outbox.drain()
lines = outbox.take_digest()
check("the digest lists every event", len(lines) == 5, lines)
check("a direct send is flagged in the digest", any("sent directly" in l for l in lines), lines)
check("the digest is cleared once taken", outbox.take_digest() == [])

print("heal result line")
what, status, commit = self_heal._parse_result(
    "lots of output\nRESULT: Browser results now come back to Chip | STATUS: built, not verified"
    " | COMMIT: abc1234\n")
check("RESULT line parsed", what == "Browser results now come back to Chip.", what)
check("STATUS parsed", status == "built, not verified", status)
check("COMMIT parsed", commit == "abc1234", commit)
check("no RESULT line → empty", self_heal._parse_result("no line here") == ("", "", ""))

if FAILS:
    print("\nFAILED:\n  " + "\n  ".join(FAILS))
    sys.exit(1)
print("\nall checks passed")
