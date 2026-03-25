#!/usr/bin/env python3
"""
Persistent Signal capture daemon.

Runs signal-cli in daemon mode with a Unix socket, reads JSON messages
from stdout as they arrive, and inserts into SQLite instantly.
"""

import json
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from signal_capture.capture import (
    ACCOUNT, DB_PATH, HEALTH_FILE, SIGNAL_CLI,
    init_db, insert_messages,
)

SOCKET_PATH = Path.home() / ".signal-capture.socket"


def send_confirmation_via_socket(count: int):
    """Send confirmation via the daemon's JSON-RPC socket."""
    if count == 0:
        return
    msg = f"[vault] {count} note{'s' if count > 1 else ''} captured."
    request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "send",
        "params": {
            "account": ACCOUNT,
            "recipient": [ACCOUNT],
            "message": msg,
        },
    }) + "\n"

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(SOCKET_PATH))
        sock.sendall(request.encode())
        sock.settimeout(5)
        response = sock.recv(4096)
        sock.close()
    except (ConnectionRefusedError, FileNotFoundError, TimeoutError) as e:
        print(f"Confirmation send failed: {e}", flush=True)


def extract_entry(msg: dict) -> dict | None:
    """Extract a Note to Self entry from a daemon JSON message."""
    envelope = msg.get("envelope", {})
    source = envelope.get("source") or envelope.get("sourceNumber", "")

    sync = envelope.get("syncMessage", {})
    sent = sync.get("sentMessage", {})
    dest = sent.get("destination") or sent.get("destinationNumber", "")
    body = sent.get("message")

    if source == ACCOUNT and dest == ACCOUNT and body:
        timestamp_ms = envelope.get("timestamp", 0)
        return {"body": body, "signal_timestamp": timestamp_ms}
    return None


def run_daemon():
    """Run signal-cli daemon and process messages as they stream in."""
    if not ACCOUNT:
        print("Error: SIGNAL_ACCOUNT not set.", file=sys.stderr)
        sys.exit(1)

    # Clean up stale socket
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    print(f"Starting signal-cli daemon for {ACCOUNT}...", flush=True)
    conn = init_db()

    proc = subprocess.Popen(
        [
            SIGNAL_CLI, "-a", ACCOUNT, "--output=json",
            "daemon", "--socket", str(SOCKET_PATH),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    print("Daemon running. Waiting for messages...", flush=True)

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Non-JSON output (log lines from signal-cli)
                continue

            entry = extract_entry(msg)
            if entry:
                inserted = insert_messages(conn, [entry])
                if inserted:
                    ts = datetime.fromtimestamp(entry["signal_timestamp"] / 1000)
                    print(f"[{ts.strftime('%H:%M')}] {entry['body'][:80]}", flush=True)
                    send_confirmation_via_socket(inserted)

            # Update health on every message cycle
            HEALTH_FILE.write_text(datetime.now().isoformat())

    except KeyboardInterrupt:
        print("\nShutting down daemon.")
    finally:
        proc.terminate()
        proc.wait()
        conn.close()
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()


def main():
    run_daemon()


if __name__ == "__main__":
    main()
