"""
Vitals — Chip Thermal Guard
====================================
Runs every 5 minutes via cron.

Thresholds:
  > 80°C : CRITICAL — pause ollama (kill signal)
  75-80°C: WARNING  — pause ollama, resume when < 70°C
  < 70°C : nominal  — ensure ollama is running
"""

import os
import subprocess
from datetime import datetime

KILL_THRESHOLD   = 80.0
WARN_THRESHOLD   = 75.0
RESUME_THRESHOLD = 70.0

def get_temp():
    try:
        out = subprocess.check_output(["vcgencmd", "measure_temp"]).decode("utf-8")
        return float(out.replace("temp=", "").replace("'C", "").strip())
    except Exception as e:
        print(f"Error reading temp: {e}")
        return 0.0

def ollama_pause():
    subprocess.run(["sudo", "-n", "pkill", "-STOP", "ollama"], capture_output=True)

def ollama_resume():
    subprocess.run(["sudo", "-n", "pkill", "-CONT", "ollama"], capture_output=True)

def main():
    temp = get_temp()
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cron = os.getenv("CRON") == "1"

    if temp >= KILL_THRESHOLD:
        print(f"[{ts}] 🔴 {temp}°C — CRITICAL. Pausing Ollama.")
        ollama_pause()

    elif temp >= WARN_THRESHOLD:
        print(f"[{ts}] 🟡 {temp}°C — WARNING. Throttling Ollama.")
        ollama_pause()

    elif temp <= RESUME_THRESHOLD:
        ollama_resume()
        if not cron:
            print(f"[{ts}] 🟢 {temp}°C — Nominal.")

    else:
        # 70-75°C — elevated but fine, just let it run
        if not cron:
            print(f"[{ts}] 🟡 {temp}°C — Elevated, within limits.")

if __name__ == "__main__":
    main()
