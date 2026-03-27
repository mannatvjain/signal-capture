#!/usr/bin/env python3
"""
Persistent Signal capture daemon.

Runs signal-cli in daemon mode with a Unix socket, reads JSON messages
from stdout as they arrive, and inserts into SQLite instantly.
"""

import json
import re
import shutil
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
from signal_capture.triage import (
    route_message, reroute_message,
    cancel_reminder_by_timestamp, reschedule_reminder_by_timestamp, parse_reschedule_time,
)

SOCKET_PATH = Path.home() / ".signal-capture.socket"
SIGNAL_CLI_ATTACHMENTS = Path.home() / ".local" / "share" / "signal-cli" / "attachments"
MEALS_DIR = Path.home() / "Documents" / "Obsidian Vaults" / "dot" / "CLAUDE" / "Running" / "Meals"

IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/heic", "image/webp"}

VALID_CATEGORIES = {"reminder", "resource", "todo", "good-advice", "founders", "deltas", "sundry"}

# Match reply corrections like "todo", "founders", "reminder"
CORRECTION_PATTERN = re.compile(
    r"^(reminder|resource|todo|good-advice|founders|deltas|sundry)$",
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

    if source == ACCOUNT and dest == ACCOUNT and (body or sent.get("attachments")):
        timestamp_ms = envelope.get("timestamp", 0)
        entry = {"body": body or "", "signal_timestamp": timestamp_ms}

        # Check if this is a reply (has quote)
        quote = sent.get("quote")
        if quote:
            entry["quote_text"] = quote.get("text", "")
            entry["quote_id"] = quote.get("id", 0)

        attachments = sent.get("attachments", [])
        if attachments:
            entry["attachments"] = attachments

        return entry
    return None


def _parse_reminder_quote(quote_text: str) -> tuple[str | None, str | None]:
    """Parse a reminder-related quote. Returns (original_body, old_category) or (None, None).

    Handles:
      [sorted] reminder @ 5:00 PM — body
      [rescheduled] 5:00 PM → 6:00 PM — body
      [cancelled] body
    """
    if quote_text.startswith("[sorted] reminder"):
        parts = quote_text.split(" — ", 1)
        if len(parts) == 2:
            old_cat = parts[0].replace("[sorted] ", "").strip()
            return parts[1], old_cat
    elif quote_text.startswith("[rescheduled]"):
        parts = quote_text.split(" — ", 1)
        if len(parts) == 2:
            return parts[1], "reminder"
    elif quote_text.startswith("[cancelled]"):
        body = quote_text.replace("[cancelled] ", "", 1).strip()
        if body:
            return body, "reminder"
    return None, None


def _is_reminder_quote(quote_text: str) -> bool:
    """Check if a quote is from a reminder-related confirmation."""
    return (
        quote_text.startswith("[sorted] reminder")
        or quote_text.startswith("[rescheduled]")
        or quote_text.startswith("[cancelled]")
    )


def _lookup_signal_timestamp(conn, original_body: str) -> int | None:
    """Look up signal_timestamp from messages table by body."""
    row = conn.execute(
        "SELECT signal_timestamp FROM messages WHERE body = ? ORDER BY signal_timestamp DESC LIMIT 1",
        (original_body,),
    ).fetchone()
    return row[0] if row else None


def handle_correction(entry: dict, conn) -> bool:
    """Handle a reply-based category correction, cancel, or reschedule. Returns True if handled."""
    if "quote_text" not in entry:
        return False

    body = entry["body"].strip()
    body_lower = body.lower()
    quote_text = entry.get("quote_text", "")

    # Check if this is a reply to a confirmation message
    is_reminder = _is_reminder_quote(quote_text)
    is_sorted_or_rerouted = quote_text.startswith("[sorted]") or quote_text.startswith("[rerouted]")

    if not is_reminder and not is_sorted_or_rerouted:
        return False

    # --- Reminder cancel/reschedule ---
    if is_reminder:
        original_body, _ = _parse_reminder_quote(quote_text)
        if not original_body:
            send_message("[error] Could not parse reminder from quote.")
            return True

        signal_timestamp = _lookup_signal_timestamp(conn, original_body)
        if not signal_timestamp:
            send_message("[error] Could not find original message.")
            return True

        # Cancel
        if body_lower == "cancel":
            result = cancel_reminder_by_timestamp(signal_timestamp)
            if result:
                print(f"Cancelled reminder: {result[:50]}", flush=True)
                send_message(f"[cancelled] {result}")
            else:
                send_message("[error] Reminder not found or already fired.")
            return True

        # If it's a category name, reroute out of reminder
        m = CORRECTION_PATTERN.match(body_lower)
        if m and m.group(1) != "reminder":
            cancel_reminder_by_timestamp(signal_timestamp)
            new_category = m.group(1).lower()
            success = reroute_message(original_body, signal_timestamp, "reminder", new_category)
            if success:
                send_message(f"[rerouted] reminder → {new_category} — {original_body}")
            else:
                send_message(f"[error] Failed to reroute to {new_category}.")
            return True

        # Otherwise, treat as reschedule time — look up current fire_at first
        import sqlite3 as _sqlite3
        from signal_capture.capture import DB_PATH as _DB_PATH
        _rconn = _sqlite3.connect(str(_DB_PATH))
        _row = _rconn.execute(
            "SELECT fire_at FROM reminders WHERE signal_timestamp = ? AND fired = 0 AND cancelled = 0",
            (signal_timestamp,),
        ).fetchone()
        _rconn.close()
        current_fire_at = _row[0] if _row else None

        new_fire_at = parse_reschedule_time(body, current_fire_at)
        if not new_fire_at:
            send_message("[error] Could not parse time for reschedule.")
            return True

        reminder_body, old_fire_at = reschedule_reminder_by_timestamp(signal_timestamp, new_fire_at)
        if reminder_body and old_fire_at:
            from datetime import datetime as dt
            try:
                old_time = dt.fromisoformat(old_fire_at).strftime("%-I:%M %p")
                new_time = dt.fromisoformat(new_fire_at).strftime("%-I:%M %p")
            except ValueError:
                old_time, new_time = old_fire_at, new_fire_at
            print(f"Rescheduled: {old_time} → {new_time}", flush=True)
            send_message(f"[rescheduled] {old_time} → {new_time} — {original_body}")
        else:
            send_message("[error] Reminder not found or already fired.")
        return True

    # --- Standard category correction (non-reminder [sorted]/[rerouted]) ---
    m = CORRECTION_PATTERN.match(body_lower)
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

    signal_timestamp = _lookup_signal_timestamp(conn, original_body)
    if not signal_timestamp:
        print(f"Could not find original message in DB: {original_body[:50]}", flush=True)
        send_message("[error] Could not find original message to reroute.")
        return True

    success = reroute_message(original_body, signal_timestamp, old_category, new_category)
    if success:
        print(f"Rerouted: {old_category} → {new_category}", flush=True)
        send_message(f"[rerouted] {old_category} → {new_category} — {original_body}")
    else:
        print(f"Reroute failed: {old_category} → {new_category}", flush=True)
        send_message(f"[error] Failed to reroute from {old_category} to {new_category}.")

    return True


def _ext_for_content_type(content_type: str) -> str:
    """Map image content type to file extension."""
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/heic": ".heic",
        "image/webp": ".webp",
    }.get(content_type, ".jpg")


def save_meal_photos(attachments: list[dict], timestamp_ms: int) -> list[Path]:
    """Copy image attachments into the Meals folder. Returns list of saved paths."""
    MEALS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")

    # Find next index for this date
    existing = sorted(MEALS_DIR.glob(f"{date_str}_*"))
    next_idx = len(existing) + 1

    saved = []
    for att in attachments:
        ct = att.get("contentType", "")
        if ct not in IMAGE_CONTENT_TYPES:
            continue

        att_id = att.get("id")
        if not att_id:
            continue

        src = SIGNAL_CLI_ATTACHMENTS / att_id
        if not src.exists():
            print(f"Attachment file not found: {src}", flush=True)
            continue

        ext = _ext_for_content_type(ct)
        dest = MEALS_DIR / f"{date_str}_{next_idx}{ext}"
        shutil.copy2(src, dest)
        saved.append(dest)
        next_idx += 1
        print(f"Saved meal photo: {dest.name}", flush=True)

    return saved


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
            if body.startswith(("[vault]", "[sorted]", "[rerouted]", "[cancelled]", "[rescheduled]", "[error]", "[meal]")):
                continue

            # Save meal photos if message has image attachments
            meal_photos = []
            if "attachments" in entry:
                image_atts = [a for a in entry["attachments"]
                              if a.get("contentType", "") in IMAGE_CONTENT_TYPES]
                if image_atts:
                    meal_photos = save_meal_photos(image_atts, entry["signal_timestamp"])
                    if meal_photos:
                        names = ", ".join(p.name for p in meal_photos)
                        send_message(f"[meal] {names}")

            # Skip messages that are photo-only with no text
            if not body:
                HEALTH_FILE.write_text(datetime.now().isoformat())
                continue

            try:
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
            except Exception as e:
                print(f"Error processing message: {e}", flush=True)
                send_message(f"[error] failed to process: {e}")

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
