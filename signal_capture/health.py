#!/usr/bin/env python3
"""
Health check for Signal capture pipeline.
Alerts via macOS notification if the last successful poll was >1 hour ago.
Run via launchd every 30 minutes.
"""

from datetime import datetime, timedelta
from pathlib import Path
import subprocess
import sys

HEALTH_FILE = Path.home() / ".signal-capture-health"
STALE_THRESHOLD = timedelta(hours=1)


def main():
    if not HEALTH_FILE.exists():
        alert("Signal capture has never run successfully.")
        sys.exit(1)

    last_run = datetime.fromisoformat(HEALTH_FILE.read_text().strip())
    age = datetime.now() - last_run

    if age > STALE_THRESHOLD:
        minutes = int(age.total_seconds() / 60)
        alert(f"Signal capture hasn't run in {minutes} minutes. Check launchd.")
        sys.exit(1)

    print(f"Healthy. Last run: {last_run.isoformat()}")


def alert(message: str):
    """Send a macOS notification."""
    print(f"ALERT: {message}", file=sys.stderr)
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "Signal Capture"'
    ], capture_output=True)


if __name__ == "__main__":
    main()
