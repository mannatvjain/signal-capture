"""
Notion routing for todos.

Sends classified todos to the "In" database. If the API call fails for
any reason (network, auth, Notion outage), the item is queued in the
local SQLite `notion_queue` table and retried by a background thread
in the daemon.
"""

import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime

from signal_capture.capture import DB_PATH

NOTION_API = "https://api.notion.com/v1/pages"
NOTION_VERSION = "2022-06-28"
IN_DATABASE_ID = "36a0e6f5-a58a-80b0-88bb-db490de36821"
REQUEST_TIMEOUT = 10


def _token() -> str | None:
    return os.environ.get("NOTION_TOKEN")


def _create_page(name: str, context: str | None) -> tuple[bool, str | None]:
    """POST to Notion. Returns (ok, error_message)."""
    token = _token()
    if not token:
        return False, "NOTION_TOKEN not set"

    properties: dict = {
        "Name": {"title": [{"text": {"content": name}}]},
    }
    if context:
        properties["Context"] = {
            "rich_text": [{"text": {"content": context}}],
        }

    payload = {
        "parent": {"database_id": IN_DATABASE_ID},
        "properties": properties,
    }

    req = urllib.request.Request(
        NOTION_API,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            if 200 <= resp.status < 300:
                return True, None
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        return False, f"HTTP {e.code}: {body}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"network: {e}"


def _enqueue(name: str, context: str | None, error: str) -> None:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        conn.execute(
            "INSERT INTO notion_queue (name, context, created_at, last_attempt_at, last_error, attempts) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (name, context, datetime.now().isoformat(), datetime.now().isoformat(), error),
        )
        conn.commit()
    finally:
        conn.close()


def send_or_queue(name: str, context: str | None) -> bool:
    """Send to Notion immediately, or queue on failure. Returns True if sent now."""
    ok, err = _create_page(name, context)
    if ok:
        print(f"Notion: sent '{name[:60]}' → In", flush=True)
        return True
    print(f"Notion send failed ({err}); queued for retry", flush=True)
    _enqueue(name, context, err or "unknown")
    return False


def drain_queue() -> int:
    """Try to send all queued items. Returns count of successful sends."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, name, context FROM notion_queue ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    sent = 0
    for row_id, name, context in rows:
        ok, err = _create_page(name, context)
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        try:
            if ok:
                conn.execute("DELETE FROM notion_queue WHERE id = ?", (row_id,))
                sent += 1
            else:
                conn.execute(
                    "UPDATE notion_queue SET last_attempt_at = ?, last_error = ?, "
                    "attempts = attempts + 1 WHERE id = ?",
                    (datetime.now().isoformat(), err, row_id),
                )
            conn.commit()
        finally:
            conn.close()
        if not ok:
            break
    if sent:
        print(f"Notion: drained {sent} queued item(s)", flush=True)
    return sent


def dequeue_by_name(name: str) -> bool:
    """Remove a queued item by exact name match. Used when a sorted todo is rerouted."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        cur = conn.execute(
            "DELETE FROM notion_queue WHERE id = (SELECT id FROM notion_queue WHERE name = ? ORDER BY id DESC LIMIT 1)",
            (name,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
