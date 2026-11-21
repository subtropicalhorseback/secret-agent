import subprocess
import random
from datetime import datetime

APPROVED_CITIES = [
    "ashburn", "boston", "chicago", "denver",
    "los angeles", "new york", "san jose", "seattle", "washington dc"
]

def rotate_node():
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Deliberate off-window (mullvad: off) — don't fight the operator.
    import os
    if os.path.exists("/run/chip/mullvad_off"):
        print(f"[{timestamp}] mullvad_off flag present — skipping rotation.")
        return
    # A scoped home-route lease is open (home_route.py). Re-pointing the exit
    # node would churn the routing table underneath a live browser session.
    # The flag carries its own expiry, so a stale one can't pause us forever.
    if os.path.exists("/run/chip/rotate_paused"):
        try:
            expiry = open("/run/chip/rotate_paused").read().strip()
        except Exception:
            expiry = ""
        if expiry and datetime.now().isoformat() > expiry:
            print(f"[{timestamp}] rotate_paused expired — ignoring stale flag.")
        else:
            print(f"[{timestamp}] rotate_paused (home-route lease) — skipping rotation.")
            return
    try:
        result = subprocess.check_output(["tailscale", "exit-node", "list"], text=True)
        valid_nodes = []
        for line in result.split("\n"):
            line_lower = line.lower()
            if "mullvad" in line_lower:
                if any(city in line_lower for city in APPROVED_CITIES):
                    parts = line.split()
                    if parts:
                        valid_nodes.append(parts[0])

        if not valid_nodes:
            print(f"[{timestamp}] ⚠️ No approved US Mullvad nodes detected online.")
            return

        target      = random.choice(valid_nodes)
        target_line = next(line for line in result.split("\n") if target in line)
        city_name   = " ".join(target_line.split()[3:5])

        print(f"[{timestamp}] 🔄 Shifting exit node to: {city_name} ({target})")

        # Use tailscale set instead of tailscale up — preserves all other flags
        subprocess.run([
            "sudo", "tailscale", "set",
            f"--exit-node={target}",
            "--exit-node-allow-lan-access"
        ], check=True)

        print(f"✅ Success. Outbound traffic now routed through {target}.")

    except Exception as e:
        print(f"[{timestamp}] ❌ Rotation failed: {e}")

if __name__ == "__main__":
    rotate_node()
