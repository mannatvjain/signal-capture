#!/usr/bin/env python3
"""
Signal → SQLite capture pipeline.

Polls signal-cli for "Note to Self" messages, inserts them into a SQLite
database in the Obsidian vault, and sends a confirmation reply.

Designed to run via launchd every 2 minutes.
"""

import subprocess
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# --- Configuration ---
VAULT_ROOT = Path.home() / "Documents" / "Obsidian Vaults" / "dot"
DB_DIR = VAULT_ROOT / "CLAUDE" / "Artifacts" / "signal-capture"
DB_PATH = DB_DIR / "capture.db"
HEALTH_FILE = Path.home() / ".signal-capture-health"
SIGNAL_CLI = "/opt/homebrew/bin/signal-cli"

CONFIG_FILE = Path(__file__).parent / ".env"
ACCOUNT = os.environ.get("SIGNAL_ACCOUNT", "")

if not ACCOUNT and CONFIG_FILE.exists():
    for line in CONFIG_FILE.read_text().splitlines():
        if line.startswith("SIGNAL_ACCOUNT="):
            ACCOUNT = line.split("=", 1)[1].strip().strip("'\"")


def init_db() -> sqlite3.Connection:
    """Initialize the database and return a connection."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_timestamp INTEGER UNIQUE NOT NULL,
            body TEXT NOT NULL,
            captured_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def receive_messages() -> list[dict]:
    """Pull pending messages from signal-cli in JSON format."""
    try:
        result = subprocess.run(
            [SIGNAL_CLI, "-a", ACCOUNT, "--output=json", "receive", "--timeout", "5"],
            capture_output=True, text=True, timeout=30
        )
        messages = []
        for line in result.stdout.strip().splitlines():
            if line.strip():
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return messages
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"Error receiving messages: {e}", file=sys.stderr)
        return []


def extract_self_messages(messages: list[dict]) -> list[dict]:
    """Filter to only 'Note to Self' messages (sender == account)."""
    captured = []
    for msg in messages:
        envelope = msg.get("envelope", {})
        source = envelope.get("source") or envelope.get("sourceNumber", "")
        data = envelope.get("dataMessage", {})
        body = data.get("message")

        if source == ACCOUNT and body:
            timestamp_ms = envelope.get("timestamp", 0)
            captured.append({"body": body, "signal_timestamp": timestamp_ms})

    return captured


def insert_messages(conn: sqlite3.Connection, entries: list[dict]) -> int:
    """Insert messages into the database. Returns count of new messages."""
    inserted = 0
    now = datetime.now().isoformat()
    for entry in entries:
        try:
            conn.execute(
                "INSERT INTO messages (signal_timestamp, body, captured_at) VALUES (?, ?, ?)",
                (entry["signal_timestamp"], entry["body"].strip(), now)
            )
            inserted += 1
        except sqlite3.IntegrityError:
            pass  # Duplicate signal_timestamp, skip
    conn.commit()
    return inserted


def send_confirmation(count: int):
    """Reply to self with a confirmation message."""
    if count == 0:
        return
    try:
        msg = f"[vault] {count} note{'s' if count > 1 else ''} captured."
        subprocess.run(
            [SIGNAL_CLI, "-a", ACCOUNT, "send", "-m", msg, ACCOUNT],
            capture_output=True, text=True, timeout=15
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def update_health():
    """Write a health timestamp so we can detect staleness."""
    HEALTH_FILE.write_text(datetime.now().isoformat())


def main():
    if not ACCOUNT:
        print("Error: SIGNAL_ACCOUNT not set.", file=sys.stderr)
        print("  Create .env with: SIGNAL_ACCOUNT='+1234567890'", file=sys.stderr)
        sys.exit(1)

    conn = init_db()
    try:
        messages = receive_messages()
        entries = extract_self_messages(messages)
        inserted = insert_messages(conn, entries)
        if inserted:
            print(f"Inserted {inserted} new messages into {DB_PATH}")
        send_confirmation(inserted)
        update_health()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
