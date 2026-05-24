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
import threading
import time
from datetime import datetime
from pathlib import Path

from signal_capture.capture import (
    ACCOUNT, DB_PATH, HEALTH_FILE, SIGNAL_CLI,
    init_db, insert_messages, send_alert,
)
from signal_capture.triage import (
    route_message, reroute_message,
    cancel_reminder_by_timestamp, reschedule_reminder_by_timestamp, parse_reschedule_time,
)
from signal_capture.cards import (
    is_card, process_card,
    is_blot, process_blot, blot_text,
    is_salience, process_salience,
)
from signal_capture.meals import IMAGE_CONTENT_TYPES, estimate_calories, resolve_attachment
from signal_capture import notion

SOCKET_PATH = Path.home() / ".signal-capture.socket"

VALID_CATEGORIES = {"reminder", "todo"}

CORRECTION_PATTERN = re.compile(r"^(reminder|todo|resource|sundry)$", re.IGNORECASE)

DEBOUNCE_SECONDS = 8

_pending_sorted: list[tuple[str, str]] = []
_pending_lock = threading.Lock()
_last_message_at: datetime | None = None


def send_message(text: str):
    """Send a Note to Self message via the daemon's JSON-RPC socket."""
    from signal_capture.capture import _recv_all
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

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(SOCKET_PATH))
        sock.sendall(request.encode())
        _recv_all(sock, timeout=5)
    except (ConnectionRefusedError, FileNotFoundError, TimeoutError, OSError) as e:
        print(f"Send failed: {e}", flush=True)
    finally:
        sock.close()


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


def _flush_pending_sorted():
    """Send all queued [sorted] confirmations as one batched message."""
    with _pending_lock:
        if not _pending_sorted:
            return
        items = list(_pending_sorted)
        _pending_sorted.clear()

    if len(items) == 1:
        category, body = items[0]
        send_message(f"[sorted] {category} — {body}")
        return

    lines = [f"[sorted] {len(items)} captures"]
    for category, body in items:
        lines.append(f"— {category}: {body}")
    send_message("\n".join(lines))


def _debounce_flusher():
    """Background thread: flush pending [sorted]s after DEBOUNCE_SECONDS of quiet."""
    while True:
        time.sleep(1)
        with _pending_lock:
            should_flush = (
                bool(_pending_sorted)
                and _last_message_at is not None
                and (datetime.now() - _last_message_at).total_seconds() >= DEBOUNCE_SECONDS
            )
        if should_flush:
            _flush_pending_sorted()


def run_daemon():
    """Run signal-cli daemon and process messages as they stream in."""
    global _last_message_at

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

    def _health_writer():
        """Periodically update health file to prove daemon is alive."""
        while True:
            HEALTH_FILE.write_text(datetime.now().isoformat())
            time.sleep(300)  # Every 5 minutes

    def _notion_drainer():
        """Periodically drain any queued Notion todos that previously failed."""
        while True:
            time.sleep(60)
            try:
                notion.drain_queue()
            except Exception as e:
                print(f"Notion drain error: {e}", flush=True)

    threading.Thread(target=_health_writer, daemon=True).start()
    threading.Thread(target=_debounce_flusher, daemon=True).start()
    threading.Thread(target=_notion_drainer, daemon=True).start()

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
            if body.startswith(("[vault]", "[sorted]", "[rerouted]", "[cancelled]", "[rescheduled]", "[error]", "[meal]", "[photo]")):
                continue

            try:
                # Insert into DB and confirm immediately
                inserted = insert_messages(conn, [entry])
                if inserted:
                    ts = datetime.fromtimestamp(entry["signal_timestamp"] / 1000)
                    print(f"[{ts.strftime('%H:%M')}] {body[:80]}", flush=True)
                    send_message(f"[vault] captured.")
                    with _pending_lock:
                        _last_message_at = datetime.now()

                # Photo + "meal" in caption → ask the model for a calorie breakdown,
                # send the answer back through Summertime. Nothing saved or logged.
                attachments = entry.get("attachments") or []
                images = [a for a in attachments if a.get("contentType") in IMAGE_CONTENT_TYPES]
                if inserted and images and "meal" in body.lower():
                    for att in images:
                        path = resolve_attachment(att)
                        if not path:
                            continue
                        breakdown = estimate_calories(path, body)
                        send_alert(f"[meal]\n{breakdown}" if breakdown else "[meal] couldn't estimate")
                    HEALTH_FILE.write_text(datetime.now().isoformat())
                    continue

                # Skip text routing for empty messages (and image-only messages
                # without the "meal" trigger word — we just ignore those now).
                if not body or (images and not body.replace(" ", "")):
                    HEALTH_FILE.write_text(datetime.now().isoformat())
                    continue

                # Cards (blot / salience / Q.A. / cloze) take priority over triage.
                # All confirmations queue through the debounce flusher.
                if inserted:
                    if is_blot(body):
                        result = process_blot(body, entry["signal_timestamp"])
                        if result == "success":
                            blot_body = re.sub(r"^\[blot\]\s*", "", body.strip(), flags=re.IGNORECASE)
                            blotted = blot_text(blot_body)
                            with _pending_lock:
                                _pending_sorted.append(("card", f"Q. {blotted}\nA. {blot_body}"))
                                _last_message_at = datetime.now()
                        elif result == "sync_failed":
                            send_message("[vault] card queued (Anki sync pending)")
                    elif is_salience(body):
                        result = process_salience(body, entry["signal_timestamp"])
                        if result == "success":
                            with _pending_lock:
                                _pending_sorted.append(("salience", body))
                                _last_message_at = datetime.now()
                        elif result == "sync_failed":
                            send_message("[vault] card queued (Anki sync pending)")
                    elif is_card(body):
                        result = process_card(body, entry["signal_timestamp"])
                        if result == "success":
                            with _pending_lock:
                                _pending_sorted.append(("card", body))
                                _last_message_at = datetime.now()
                        elif result == "sync_failed":
                            send_message("[vault] card queued (Anki sync pending)")
                    else:
                        category = route_message(body, entry["signal_timestamp"])
                        if category:
                            with _pending_lock:
                                _pending_sorted.append((category, body))
                                _last_message_at = datetime.now()
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
