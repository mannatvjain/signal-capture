#!/usr/bin/env python3
"""
signal-capture CLI.

Usage:
    signal-capture poll       Pull new messages from Signal into the database
    signal-capture view       Open the TUI message viewer
    signal-capture health     Check pipeline health
    signal-capture list       Print recent messages to stdout
    signal-capture count      Print message count
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from signal_capture.capture import DB_PATH, init_db


def cmd_poll(args):
    """Pull new messages from Signal."""
    from signal_capture.capture import (
        ACCOUNT, receive_messages, extract_self_messages,
        insert_messages, send_confirmation, update_health
    )
    if not ACCOUNT:
        print("Error: SIGNAL_ACCOUNT not set. Create .env in the project root.", file=sys.stderr)
        sys.exit(1)

    conn = init_db()
    try:
        messages = receive_messages()
        entries = extract_self_messages(messages)
        inserted = insert_messages(conn, entries)
        if inserted:
            print(f"Captured {inserted} new message{'s' if inserted != 1 else ''}.")
        else:
            print("No new messages.")
        send_confirmation(inserted)
        update_health()
    finally:
        conn.close()


def cmd_view(args):
    """Open the TUI viewer."""
    if not DB_PATH.exists():
        print("Database not found. Run `signal-capture poll` first.", file=sys.stderr)
        sys.exit(1)

    from signal_capture.viewer import SignalViewer
    app = SignalViewer()
    app.run()


def cmd_health(args):
    """Check pipeline health."""
    from signal_capture.health import main as health_main
    health_main()


def cmd_list(args):
    """Print recent messages to stdout."""
    if not DB_PATH.exists():
        print("Database not found. Run `signal-capture poll` first.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    limit = args.limit or 20
    rows = conn.execute(
        "SELECT signal_timestamp, body, captured_at FROM messages "
        "ORDER BY signal_timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        print("No messages.")
        return

    max_width = 70
    current_date = None

    for ts, body, captured_at in rows:
        dt = datetime.fromtimestamp(ts / 1000)
        date_str = dt.strftime("%b %d")

        if date_str != current_date:
            current_date = date_str
            print(f"  {date_str}")

        oneline = body.replace("\n", " ↩ ")
        if len(oneline) > max_width:
            oneline = oneline[:max_width - 1] + "…"
        print(f"    {dt.strftime('%H:%M')}  {oneline}")


def cmd_daemon(args):
    """Run persistent capture daemon."""
    from signal_capture.daemon import run_daemon
    run_daemon()


def cmd_count(args):
    """Print message count."""
    if not DB_PATH.exists():
        print("0")
        return

    conn = sqlite3.connect(str(DB_PATH))
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    print(count)


def main():
    parser = argparse.ArgumentParser(
        prog="sl",
        description="Signal capture pipeline",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("poll", help="Pull new messages from Signal into the database")
    sub.add_parser("daemon", help="Run persistent capture daemon (instant delivery)")
    sub.add_parser("view", help="Open the TUI message viewer")
    sub.add_parser("health", help="Check pipeline health")

    list_parser = sub.add_parser("list", help="Print recent messages to stdout")
    list_parser.add_argument("-n", "--limit", type=int, default=20, help="Number of messages (default: 20)")

    sub.add_parser("count", help="Print total message count")

    args = parser.parse_args()

    commands = {
        "poll": cmd_poll,
        "daemon": cmd_daemon,
        "view": cmd_view,
        "health": cmd_health,
        "list": cmd_list,
        "count": cmd_count,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
