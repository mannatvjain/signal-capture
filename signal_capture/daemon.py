#!/usr/bin/env python3
"""
Persistent Signal capture daemon.

Runs signal-cli in daemon mode with a Unix socket, reads JSON messages
from stdout as they arrive, and inserts into SQLite instantly.
"""

import json
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from signal_capture.capture import (
    ACCOUNT, DB_PATH, HEALTH_FILE, SIGNAL_CLI,
    init_db, insert_messages,
)
from signal_capture.cards import process_card, is_card
from signal_capture.triage import route_message, reroute_message

SOCKET_PATH = Path.home() / ".signal-capture.socket"

VALID_CATEGORIES = {"resource", "todo", "good-advice", "founders", "deltas", "sundry"}

# Match reply corrections like "todo", "founders", "good-advice"
CORRECTION_PATTERN = re.compile(
    r"^(resource|todo|good-advice|founders|deltas|sundry)$",
    re.IGNORECASE,
)


def send_message(text: str):
    """Send a Note to Self message via the daemon's JSON-RPC socket."""
    request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "send",
        "params": {
            "account": ACCOUNT,
            "recipient": [ACCOUNT],
            "message": text,
        },
    }) + "\n"

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(SOCKET_PATH))
        sock.sendall(request.encode())
        sock.settimeout(5)
        sock.recv(4096)
        sock.close()
    except (ConnectionRefusedError, FileNotFoundError, TimeoutError, OSError) as e:
        print(f"Send failed: {e}", flush=True)


def extract_entry(msg: dict) -> dict | None:
    """Extract a Note to Self entry from a daemon JSON message.

    Returns dict with body, signal_timestamp, and optionally quote info.
    """
    envelope = msg.get("envelope", {})
    source = envelope.get("source") or envelope.get("sourceNumber", "")

    sync = envelope.get("syncMessage", {})
    sent = sync.get("sentMessage", {})
    dest = sent.get("destination") or sent.get("destinationNumber", "")
    body = sent.get("message")

    if source == ACCOUNT and dest == ACCOUNT and body:
        timestamp_ms = envelope.get("timestamp", 0)
        entry = {"body": body, "signal_timestamp": timestamp_ms}

        # Check if this is a reply (has quote)
        quote = sent.get("quote")
        if quote:
            entry["quote_text"] = quote.get("text", "")
            entry["quote_id"] = quote.get("id", 0)

        return entry
    return None


def handle_correction(entry: dict, conn) -> bool:
    """Handle a reply-based category correction. Returns True if handled."""
    if "quote_text" not in entry:
        return False

    body = entry["body"].strip().lower()
    quote_text = entry.get("quote_text", "")

    # Check if this is a correction reply to a [sorted] or [rerouted] message
    if not (quote_text.startswith("[sorted]") or quote_text.startswith("[rerouted]")):
        return False

    m = CORRECTION_PATTERN.match(body)
    if not m:
        return False

    new_category = m.group(1).lower()

    if quote_text.startswith("[sorted]"):
        # Format: "[sorted] category — original_body"
        parts = quote_text.split(" — ", 1)
        if len(parts) < 2:
            print(f"Could not parse original message from quote: {quote_text}", flush=True)
            return False
        original_body = parts[1]
        old_category = parts[0].replace("[sorted] ", "").strip()
        if old_category == "card":
            print("Ignoring correction on card message.", flush=True)
            send_message("[error] Cards can't be rerouted.")
            return True
    else:
        # Format: "[rerouted] old → new — original_body"
        parts = quote_text.split(" — ", 1)
        if len(parts) < 2:
            print(f"Could not parse original message from quote: {quote_text}", flush=True)
            return False
        original_body = parts[1]
        # The current category is the one after the arrow
        arrow_parts = parts[0].replace("[rerouted] ", "").split(" → ")
        old_category = arrow_parts[-1].strip() if len(arrow_parts) >= 2 else arrow_parts[0].strip()

    # Look up the signal_timestamp from DB by body match
    import sqlite3
    row = conn.execute(
        "SELECT signal_timestamp FROM messages WHERE body = ? ORDER BY signal_timestamp DESC LIMIT 1",
        (original_body,),
    ).fetchone()

    if not row:
        print(f"Could not find original message in DB: {original_body[:50]}", flush=True)
        send_message(f"[error] Could not find original message to reroute.")
        return True

    signal_timestamp = row[0]

    success = reroute_message(original_body, signal_timestamp, old_category, new_category)
    if success:
        print(f"Rerouted: {old_category} → {new_category}", flush=True)
        send_message(f"[rerouted] {old_category} → {new_category} — {original_body}")
    else:
        print(f"Reroute failed: {old_category} → {new_category}", flush=True)
        send_message(f"[error] Failed to reroute from {old_category} to {new_category}.")

    return True


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
                continue

            entry = extract_entry(msg)
            if not entry:
                continue

            # Check for correction replies first (don't insert these into DB)
            if handle_correction(entry, conn):
                HEALTH_FILE.write_text(datetime.now().isoformat())
                continue

            # Skip our own confirmation messages
            body = entry["body"]
            if body.startswith("[vault]") or body.startswith("[sorted]") or body.startswith("[rerouted]") or body.startswith("[error]"):
                continue

            inserted = insert_messages(conn, [entry])
            if inserted:
                ts = datetime.fromtimestamp(entry["signal_timestamp"] / 1000)
                print(f"[{ts.strftime('%H:%M')}] {body[:80]}", flush=True)

                # Confirmation 1: captured
                send_message(f"[vault] captured.")

                if is_card(body):
                    process_card(body, entry["signal_timestamp"])
                    send_message(f"[sorted] card — {body}")
                else:
                    category = route_message(body, entry["signal_timestamp"])
                    if category:
                        send_message(f"[sorted] {category} — {body}")

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
