#!/usr/bin/env python3
"""
Signal → SQLite capture pipeline.

Polls signal-cli for "Note to Self" messages, inserts them into a SQLite
database in the Obsidian vault, and sends a confirmation reply.

Designed to run via launchd every 2 minutes.
"""

import socket
import subprocess
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# --- Configuration ---
VAULT_ROOT = Path.home() / "Documents" / "dot"
DB_DIR = VAULT_ROOT / "CLAUDE" / "Artifacts" / "signal-capture"
DB_PATH = DB_DIR / "capture.db"
HEALTH_FILE = Path.home() / ".signal-capture-health"
SIGNAL_CLI = "/opt/homebrew/bin/signal-cli"

CONFIG_FILE = Path(__file__).parent.parent / ".env"
ACCOUNT = os.environ.get("SIGNAL_ACCOUNT", "")
ALERT_ACCOUNT = os.environ.get("SIGNAL_ALERT_ACCOUNT", "")
ALERT_RECIPIENT = os.environ.get("SIGNAL_ALERT_RECIPIENT", "")

if CONFIG_FILE.exists():
    for line in CONFIG_FILE.read_text().splitlines():
        key, _, val = line.partition("=")
        val = val.strip().strip("'\"")
        if key == "SIGNAL_ACCOUNT" and not ACCOUNT:
            ACCOUNT = val
        elif key == "SIGNAL_ALERT_ACCOUNT" and not ALERT_ACCOUNT:
            ALERT_ACCOUNT = val
        elif key == "SIGNAL_ALERT_RECIPIENT" and not ALERT_RECIPIENT:
            ALERT_RECIPIENT = val


SUMMERTIME_ALERT_SOCKET = Path.home() / ".summertime-alert.socket"


def _recv_all(sock: socket.socket, timeout: float = 10) -> bytes:
    """Read from socket until a full JSON response is received."""
    sock.settimeout(timeout)
    chunks = []
    while True:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            try:
                json.loads(b"".join(chunks))
                break
            except json.JSONDecodeError:
                continue
        except socket.timeout:
            break
    return b"".join(chunks)


def _send_via_socket(text: str, attachments: list[str] | None = None) -> int | None:
    """Send via Summertime's alert daemon socket (preferred when Summertime is running)."""
    if not SUMMERTIME_ALERT_SOCKET.exists():
        return None
    params = {
        "account": ALERT_ACCOUNT,
        "recipient": [ALERT_RECIPIENT],
        "message": text,
    }
    if attachments:
        params["attachments"] = attachments
    request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "send",
        "params": params,
    }) + "\n"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(SUMMERTIME_ALERT_SOCKET))
        sock.sendall(request.encode())
        data = _recv_all(sock)
        resp = json.loads(data.decode())
        result = resp.get("result", {})
        return result.get("timestamp") or int(datetime.now().timestamp() * 1000)
    except (ConnectionRefusedError, FileNotFoundError, TimeoutError, OSError) as e:
        print(f"Socket send failed, falling back to subprocess: {e}", file=sys.stderr)
        return None
    except (json.JSONDecodeError, AttributeError):
        return int(datetime.now().timestamp() * 1000)
    finally:
        sock.close()


def send_alert(text: str, attachments: list[str] | None = None) -> int | None:
    """Send a push notification via the GV bot account.

    Uses Summertime's alert socket when available (avoids signal-cli lock conflict),
    falls back to direct subprocess.

    Returns the sent message's Signal timestamp (ms) on success, None on failure.
    """
    if not ALERT_ACCOUNT or not ALERT_RECIPIENT:
        print("Alert account not configured.", file=sys.stderr)
        return None

    # Prefer socket (Summertime's daemon already holds the signal-cli lock)
    result = _send_via_socket(text, attachments)
    if result is not None:
        return result

    # Fallback: direct signal-cli subprocess
    try:
        cmd = [SIGNAL_CLI, "-a", ALERT_ACCOUNT, "--output=json", "send", "-m", text, ALERT_RECIPIENT]
        if attachments:
            for a in attachments:
                cmd.extend(["--attachment", a])
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout.strip().splitlines()[-1])
                return data.get("timestamp") or int(datetime.now().timestamp() * 1000)
            except (json.JSONDecodeError, IndexError):
                return int(datetime.now().timestamp() * 1000)
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"Alert send failed: {e}", file=sys.stderr)
        return None


def init_db() -> sqlite3.Connection:
    """Initialize the database and return a connection."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_timestamp INTEGER UNIQUE NOT NULL,
            body TEXT NOT NULL,
            captured_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            body TEXT NOT NULL,
            fire_at TEXT NOT NULL,
            signal_timestamp INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            fired INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Migration: add cancelled column if missing
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reminders)").fetchall()}
    if "cancelled" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0")
    # Migration: add obsidian_synced column for card retry tracking
    # NULL = not a card, 0 = card pending sync, 1 = card synced to Obsidian
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "obsidian_synced" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN obsidian_synced INTEGER")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notion_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            context TEXT,
            created_at TEXT NOT NULL,
            last_attempt_at TEXT,
            last_error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meal_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_name TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            content_type TEXT,
            queued_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Migration: add attempts column for estimate retry tracking
    mp_cols = {r[1] for r in conn.execute("PRAGMA table_info(meal_pending)").fetchall()}
    if "attempts" not in mp_cols:
        conn.execute("ALTER TABLE meal_pending ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
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

        # Note to Self arrives as a syncMessage.sentMessage to yourself
        sync = envelope.get("syncMessage", {})
        sent = sync.get("sentMessage", {})
        dest = sent.get("destination") or sent.get("destinationNumber", "")
        body = sent.get("message")

        if source == ACCOUNT and dest == ACCOUNT and (body or sent.get("attachments")):
            timestamp_ms = envelope.get("timestamp", 0)
            entry = {"body": body or "", "signal_timestamp": timestamp_ms}
            attachments = sent.get("attachments", [])
            if attachments:
                entry["attachments"] = attachments
            captured.append(entry)

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
